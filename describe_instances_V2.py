import boto3
import json
import logging
from datetime import datetime, timezone

# Lambda already attaches a CloudWatch handler; basicConfig only takes effect on local runs (stderr)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger()
logger.setLevel(logging.INFO)
logging.getLogger("botocore").setLevel(logging.WARNING)


def log_event(event, **fields):
    # One flat JSON line per event so CloudWatch Logs Insights can filter by field
    logger.info(json.dumps({"event": event, **fields}, default=str))


# Inputs
region = "us-east-1"
families = ["g5", "g6", "t3", "c7i"]
# UTC, DD-MM-YYYY HH:MM
end_date_utc = "30-11-2026 23:59"

# formatting to utc format
requested_end = datetime.strptime(end_date_utc, "%d-%m-%Y %H:%M").replace(tzinfo=timezone.utc)

# ISO 8601 form required by EC2 CreateCapacityReservation
end_date_iso = requested_end.strftime("%Y-%m-%dT%H:%M:%SZ")

log_event("runStarted", region=region, families=families,
          endDateUtc=end_date_utc, endDateIso=end_date_iso)


def expires_before_requested_end(odcr):
    # Unknown ODCR (empty dict) has no end date info -> "yes" so it gets attention
    if odcr.get("EndDateType") == "unlimited":
        return "no"
    odcr_end = odcr.get("EndDate")
    return "no" if odcr_end and odcr_end >= requested_end else "yes"


# Create EC2 client
ec2 = boto3.client("ec2", region_name=region)

# Instances that need a new ODCR
instances_needing_odcr = []

# Running instances with ODCR associated; expiresBeforeRequestedEndDate says if that ODCR ends before end_date_utc
instances_with_existing_odcr = []

# Instances ineligible for an ODCR (preference "none" or targeted), reported via SNS
ineligible_instances = []

# Existing active ODCRs with unused slots, reported to the ops team via SNS email. 
unused_capacity_reservations = []

# Spot / Capacity Block instances seen and skipped (count only, for the run summary)
skipped_lifecycle_count = 0

# Gather all active ODCRs by ID, to look up the end date of an ODCR a running instance already uses
# collect all active unused ODCR for reporting to the ops team - ideally it should be none.
active_odcrs = {}

for cr_page in ec2.get_paginator("describe_capacity_reservations").paginate(
    Filters=[
        {
            "Name": "state",
            "Values": ["active"]
        }
    ]
):

    for cr in cr_page["CapacityReservations"]:

        active_odcrs[cr["CapacityReservationId"]] = cr

        if cr["AvailableInstanceCount"] == 0:
            continue

        unused_capacity_reservations.append({
            "capacityReservationId": cr["CapacityReservationId"],
            "instanceType": cr["InstanceType"],
            "instancePlatform": cr["InstancePlatform"],
            "availabilityZone": cr["AvailabilityZone"],
            "tenancy": cr["Tenancy"],
            "instanceMatchCriteria": cr["InstanceMatchCriteria"],
            "totalInstanceCount": cr["TotalInstanceCount"],
            "availableInstanceCount": cr["AvailableInstanceCount"],
            "endDateType": cr["EndDateType"],
            # Absent for unlimited ODCRs; datetime -> string for JSON output
            "endDate": cr["EndDate"].isoformat() if cr.get("EndDate") else None,
            "expiresBeforeRequestedEndDate": expires_before_requested_end(cr)
        })

        log_event("unusedOdcr", **unused_capacity_reservations[-1])

log_event("activeOdcrsCollected", activeOdcrCount=len(active_odcrs),
          unusedOdcrCount=len(unused_capacity_reservations))

# Iterate over all running and stopped instances matching the filters to determine their ODCR status

for page in ec2.get_paginator("describe_instances").paginate(
    IncludeManagedResources=False,
    Filters=[
        {
            "Name": "instance-state-name",
            "Values": ["running", "stopped"]
        },
        {
            "Name": "tenancy",
            "Values": ["default"]
        },
        {
            # g5 -> g5.* (matches g5.xlarge, g5.2xlarge, ...)
            "Name": "instance-type",
            "Values": [f"{f}.*" for f in families]
        }
    ]
):

    for reservation in page["Reservations"]:

        for instance in reservation["Instances"]:

            instance_type = instance["InstanceType"]

            # Spot / Capacity Block instances never consume an ODCR
            # InstanceLifecycle is absent (None) for On-Demand -> fall through and process
            # "spot" / "capacity-block" -> continue skips to the next instance
            if instance.get("InstanceLifecycle"):
                skipped_lifecycle_count += 1
                log_event("instanceDecision", decision="skipped-lifecycle",
                          instanceId=instance["InstanceId"], instanceType=instance_type,
                          lifecycle=instance["InstanceLifecycle"])
                continue

            state = instance["State"]["Name"]

            # Returns Capacity Reservation ID if present,
            # otherwise returns None
            capacity_reservation_id = instance.get(
                "CapacityReservationId"
            )

            # ------------------------------
            # Already has Capacity Reservation
            # ------------------------------
            if state == "running" and capacity_reservation_id:

                odcr = active_odcrs.get(capacity_reservation_id, {})
                odcr_end = odcr.get("EndDate")

# Add instance to the list of instances with an existing ODCR, this helps track which instances are already covered
# This list will send as email notification to the ops team as part of the workflow in sfn.
                instances_with_existing_odcr.append({
                    "instanceId": instance["InstanceId"],
                    "state": state,
                    "instanceType": instance_type,
                    "availabilityZone":
                        instance["Placement"]["AvailabilityZone"],
                    "instancePlatform":
                        instance["PlatformDetails"],
                    "tenancy":
                        instance["Placement"]["Tenancy"],
                    "capacityReservationId":
                        capacity_reservation_id,
                    "odcrEndDateType": odcr.get("EndDateType"),
                    "odcrEndDate": odcr_end.isoformat() if odcr_end else None,
                    "expiresBeforeRequestedEndDate": expires_before_requested_end(odcr)
                })

                log_event("instanceDecision", decision="existing-odcr",
                          **instances_with_existing_odcr[-1])

                continue

            # ------------------------------
            # Instances Won't use an open ODCR -> creating one would be wasted
            # These instances have a CapacityReservationPreference other than "open" 
            # and are therefore ineligible for an open ODCR
            # This will send in the email notification to the ops team as part of the workflow in sfn.
            # ------------------------------
            # Targeted instances have CapacityReservationTarget instead of a preference
            cr_spec = instance.get("CapacityReservationSpecification", {})

            if cr_spec.get("CapacityReservationPreference") != "open":

                ineligible_instances.append({
                    "instanceId": instance["InstanceId"],
                    "state": state,
                    "instanceType": instance_type,
                    "availabilityZone":
                        instance["Placement"]["AvailabilityZone"],
                    "instancePlatform":
                        instance["PlatformDetails"],
                    "tenancy":
                        instance["Placement"]["Tenancy"],
                    "capacityReservationSpecification": cr_spec
                })

                log_event("instanceDecision", decision="ineligible",
                          instanceId=instance["InstanceId"], state=state,
                          instanceType=instance_type,
                          availabilityZone=instance["Placement"]["AvailabilityZone"],
                          preference=cr_spec.get("CapacityReservationPreference"),
                          target=cr_spec.get("CapacityReservationTarget"))

                continue

            # Not handled by design: stopped instances already covered by an existing ODCR are
            # not detected. ODCR lifecycle is aligned to the patching window, and unused ODCRs
            # are alerted via CloudWatch and reported in unusedCapacityReservations for manual action.

            # ------------------------------
            # Needs a new ODCR - findout the instances and details that requires a new reservation
            # only for these instances the ODCR needs to be created rest all the details collected for email
            # notification to the ops team via the Step Function workflow
            # ------------------------------
            instances_needing_odcr.append({
                "instanceId": instance["InstanceId"],
                "state": state,
                "instanceType": instance_type,
                "availabilityZone":
                    instance["Placement"]["AvailabilityZone"],
                "instancePlatform":
                    instance["PlatformDetails"],
                "tenancy":
                    instance["Placement"]["Tenancy"],
                "instanceCount": 1,
                "instanceMatchCriteria": "open",
                "endDateType": "limited",
                "endDate": end_date_iso
            })

            log_event("instanceDecision", decision="needs-odcr",
                      **instances_needing_odcr[-1])


# ------------------------------
# Build ODCR requests for the Step Function Map (running groups first, then stopped)
# Running -> aggregated per (type, platform, AZ, tenancy); capacity is already held
# Stopped -> one request per instance; CreateCapacityReservation is all-or-nothing
# ------------------------------
running_groups = {}
stopped_requests = []

for inst in instances_needing_odcr:

    if inst["state"] == "running":

        key = (
            inst["instanceType"],
            inst["instancePlatform"],
            inst["availabilityZone"],
            inst["tenancy"]
        )

        group = running_groups.setdefault(key, {
            "state": "running",
            "instanceType": inst["instanceType"],
            "instancePlatform": inst["instancePlatform"],
            "availabilityZone": inst["availabilityZone"],
            "tenancy": inst["tenancy"],
            "instanceCount": 0,
            "instanceMatchCriteria": "open",
            "endDateType": "limited",
            "endDate": end_date_iso,
            "instanceIds": []
        })

        group["instanceCount"] += 1
        group["instanceIds"].append(inst["instanceId"])

    else:

        stopped_requests.append({
            "state": "stopped",
            "instanceType": inst["instanceType"],
            "instancePlatform": inst["instancePlatform"],
            "availabilityZone": inst["availabilityZone"],
            "tenancy": inst["tenancy"],
            "instanceCount": 1,
            "instanceMatchCriteria": "open",
            "endDateType": "limited",
            "endDate": end_date_iso,
            "instanceIds": [inst["instanceId"]]
        })

capacity_reservation_requests = list(running_groups.values()) + stopped_requests

for req in capacity_reservation_requests:
    log_event("odcrRequest", **req)

log_event(
    "runSummary",
    skippedLifecycle=skipped_lifecycle_count,
    instancesWithExistingOdcr=len(instances_with_existing_odcr),
    existingOdcrExpiringBeforeEndDate=sum(
        1 for i in instances_with_existing_odcr
        if i["expiresBeforeRequestedEndDate"] == "yes"
    ),
    ineligibleInstances=len(ineligible_instances),
    instancesNeedingOdcr=len(instances_needing_odcr),
    odcrRequests=len(capacity_reservation_requests),
    runningOdcrRequests=len(running_groups),
    stoppedOdcrRequests=len(stopped_requests),
    unusedOdcrs=len(unused_capacity_reservations)
)


# Final result
result = {
    "capacityReservationRequests": capacity_reservation_requests,
    "instancesWithExistingOdcr": instances_with_existing_odcr,
    "ineligibleInstances": ineligible_instances,
    "unusedCapacityReservations": unused_capacity_reservations
}


# Display JSON
print(json.dumps(result, indent=2))
