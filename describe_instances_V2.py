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
    "skippedInstances": skipped_instances
}


# Display JSON
print(json.dumps(result, indent=2))
