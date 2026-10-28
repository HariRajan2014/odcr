import boto3
import json
from datetime import datetime, timezone


# Inputs
region = "us-east-1"
families = ["g5", "g6", "t3", "c7i"]
# UTC, DD-MM-YYYY HH:MM
end_date_utc = "30-11-2026 23:59"

# formatting to utc format
requested_end = datetime.strptime(end_date_utc, "%d-%m-%Y %H:%M").replace(tzinfo=timezone.utc)

# ISO 8601 form required by EC2 CreateCapacityReservation
end_date_iso = requested_end.strftime("%Y-%m-%dT%H:%M:%SZ")


def expires_before_requested_end(odcr):
    # Unknown ODCR (empty dict) has no end date info -> "yes" so it gets attention
    if odcr.get("EndDateType") == "unlimited":
        return "no"
    odcr_end = odcr.get("EndDate")
    return "no" if odcr_end and odcr_end >= requested_end else "yes"


# Create EC2 client
ec2 = boto3.client("ec2", region_name=region)

# Instances that need a new ODCR
instances = []

# Running instances with ODCR associated; expiresBeforeRequestedEndDate says if that ODCR ends before end_date_utc
instances_with_existing_odcr = []

# Instances ineligible for an ODCR (preference "none" or targeted), reported via SNS
ineligible_instances = []

# Existing active ODCRs with unused slots, reported to the ops team via SNS email. 
unused_capacity_reservations = []

# Gather all active ODCRs by ID, to look up the end date of an ODCR a running instance already uses
#
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

                continue

            # ------------------------------
            # Won't use an open ODCR -> creating one would be wasted
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

                continue

            # Not handled by design: stopped instances already covered by an existing ODCR are
            # not detected. ODCR lifecycle is aligned to the patching window, and unused ODCRs
            # are alerted via CloudWatch and reported in unusedCapacityReservations for manual action.

            # ------------------------------
            # Needs a new ODCR
            # ------------------------------
            instances.append({
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


# ------------------------------
# Build ODCR requests for the Step Function Map (running groups first, then stopped)
# Running -> aggregated per (type, platform, AZ, tenancy); capacity is already held
# Stopped -> one request per instance; CreateCapacityReservation is all-or-nothing
# ------------------------------
running_groups = {}
stopped_requests = []

for inst in instances:

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


# Final result
result = {
    "capacityReservationRequests": capacity_reservation_requests,
    "instances": instances,
    "instancesWithExistingOdcr": instances_with_existing_odcr,
    "ineligibleInstances": ineligible_instances,
    "unusedCapacityReservations": unused_capacity_reservations
}


# Display JSON
print(json.dumps(result, indent=2))
