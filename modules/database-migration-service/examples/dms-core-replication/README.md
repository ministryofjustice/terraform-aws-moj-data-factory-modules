# DMS Core Replication Example

This example shows how to use the `dms-core` module to create the replication
infrastructure needed by AWS DMS.

It uses existing VPC and subnet infrastructure supplied by the caller.

In this example the module creates the DMS replication subnet group from the
provided subnet IDs.

It does not create any DMS endpoints or replication tasks. Those will be added
to the reusable DMS capability separately.

This example is not connected to any existing Data Factory or HMPPS deployment
and is intended to demonstrate and validate the module interface only.
