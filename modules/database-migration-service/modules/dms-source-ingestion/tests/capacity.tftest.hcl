mock_provider "aws" {
  mock_data "aws_region" {
    defaults = {
      region = "eu-west-2"
    }
  }

  mock_data "aws_caller_identity" {
    defaults = {
      account_id = "111122223333"
    }
  }

  mock_data "aws_iam_policy_document" {
    defaults = {
      json = jsonencode({
        Version   = "2012-10-17"
        Statement = []
      })
    }
  }
}

variables {
  name        = "sandbox-source"
  environment = "development"
  vpc_id      = "vpc-0123456789abcdef0"

  network = {
    subnet_ids = [
      "subnet-0123456789abcdef0",
      "subnet-0123456789abcdef1",
    ]
  }

  replication_instance = {
    instance_class    = "dms.t3.small"
    allocated_storage = 20
  }

  source_endpoint = {
    engine        = "postgres"
    database_name = "example"
    secret_arn    = "arn:aws:secretsmanager:eu-west-2:111122223333:secret:example-AbCdEf"
  }

  target = {
    bucket_name   = "example-landing"
    bucket_folder = "generation-one"
  }

  replication_tasks = {
    integration = {
      replication_task_id = "sandbox-integration"
      migration_type      = "full-load-and-cdc"

      table_mappings = jsonencode({
        rules = []
      })
    }
  }

  existing_replication_capacity = {
    vpc_id                   = "vpc-0123456789abcdef0"
    replication_instance_arn = "arn:aws:dms:eu-west-2:111122223333:rep:SHAREDPOOL"
    replication_instance_id  = "sandbox-shared"

    replication_security_group_id = "sg-0123456789abcdef0"

    replication_security_group_ids = [
      "sg-0123456789abcdef0",
    ]

    replication_subnet_group_id = "sandbox-shared-subnets"

    task_log_group = {
      name = "dms-tasks-sandbox-shared"
      arn  = "arn:aws:logs:eu-west-2:111122223333:log-group:dms-tasks-sandbox-shared"
    }
  }
}

run "tasks_use_platform_capacity" {
  command = plan

  assert {
    condition = (
      output.deployment_binding.replication_tasks.integration.replication_instance_arn
      == "arn:aws:dms:eu-west-2:111122223333:rep:SHAREDPOOL"
    )

    error_message = "Tasks must be placed on the approved shared instance."
  }

  assert {
    condition = (
      output.replication_task_log_group.name
      == "dms-tasks-sandbox-shared"
    )

    error_message = "Task groups must use the capacity owner's log group."
  }
}

run "reject_missing_source_network_access" {
  command = plan

  variables {
    network = {
      subnet_ids = [
        "subnet-0123456789abcdef0",
        "subnet-0123456789abcdef1",
      ]

      additional_security_group_ids = [
        "sg-00000000000000000",
      ]
    }
  }

  expect_failures = [
    var.existing_replication_capacity,
  ]
}

run "reject_other_vpc" {
  command = plan

  variables {
    vpc_id = "vpc-00000000000000000"
  }

  expect_failures = [
    var.existing_replication_capacity,
  ]
}
