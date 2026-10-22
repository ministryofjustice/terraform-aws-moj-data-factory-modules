terraform {
  required_version = ">= 1.7.0, < 2.0.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.42"
    }
  }
}

variable "compiled_artifact" {
  description = "Compiler output supplied by the compatibility tests."
  type        = any
}

provider "aws" {
  region = var.compiled_artifact.runtime_configuration.region
}

module "source_ingestion" {
  for_each = var.compiled_artifact.module_inputs

  source = "../../../modules/dms-source-ingestion"

  name        = each.value.name
  environment = each.value.environment
  vpc_id      = each.value.vpc_id

  network              = each.value.network
  replication_instance = each.value.replication_instance
  source_endpoint      = each.value.source_endpoint
  target               = each.value.target
  replication_tasks    = each.value.replication_tasks
}

output "replication_tasks" {
  description = "Task identities exposed by the actual ingestion module."

  value = {
    for group_name, deployment in module.source_ingestion :
    group_name => deployment.replication_tasks
  }
}
