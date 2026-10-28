# DMS replication capacity

This module owns a DMS replication instance and its shared operational
resources: security group, subnet group where required, alarms and task
log group.

It does not create source endpoints, target endpoints or replication tasks.

The platform can deploy it once and pass its binding output to several
dms-source-ingestion deployments. Those deployments keep separate endpoints,
tasks and landing locations while sharing approved replication capacity.

The capacity owner controls sizing, networking, encryption, logs and alarms.
Required source-access security groups must be attached to the capacity.

The existing dms-core module also uses this module internally for its
default dedicated-instance behaviour. Explicit moved blocks preserve the
previous resource addresses when upgrading in that mode.

Switching an existing dedicated deployment to shared capacity is a separate
migration. It must not be done by simply changing the optional input.

Pool placement does not provide workload admission or quota reservation.
Those checks must be connected before enabling automatic source starts.

Account-level DMS service roles and source connectivity remain prerequisites.