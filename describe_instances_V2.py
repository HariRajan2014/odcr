import boto3
import json


# Inputs
region = "eu-west-1"
families = ["g5", "g6", "t3", "c7i"]
end_date = "2026-10-26T23:59:00Z"


# Create EC2 client
ec2 = boto3.client("ec2", region_name=region)

# Paginator for DescribeInstances
paginator = ec2.get_paginator("describe_instances")

# Instances that need a new ODCR
instances = []

# Running instances that already have a Capacity Reservation
skipped_instances = []

# Existing active ODCRs with unused slots, reported to the ops team via SNS
unused_capacity_reservations = []

for cr_page in ec2.get_paginator("describe_capacity_reservations").paginate(
    Filters=[
        {
            "Name": "state",
            "Values": ["active"]
        }
    ]
):

    for cr in cr_page["CapacityReservations"]:

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
            "endDate": cr["EndDate"].isoformat() if cr.get("EndDate") else None
        })


for page in paginator.paginate(
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

                skipped_instances.append({
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
                        capacity_reservation_id
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
                "endDate": end_date
            })


# Final result
result = {
    "instances": instances,
    "skippedInstances": skipped_instances,
    "unusedCapacityReservations": unused_capacity_reservations
}


# Display JSON
print(json.dumps(result, indent=2))
