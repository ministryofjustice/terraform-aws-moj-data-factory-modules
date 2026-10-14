locals {
  lambda_alarm_metrics = {
    errors = {
      metric_name = "Errors"
      description = "Schema generation returned an invocation error."
    }
    throttles = {
      metric_name = "Throttles"
      description = "Schema generation exceeded available concurrency."
    }
    async-events-dropped = {
      metric_name = "AsyncEventsDropped"
      description = "An asynchronous generation event was dropped."
    }
    destination-delivery-failures = {
      metric_name = "DestinationDeliveryFailures"
      description = "Lambda could not deliver an invocation record to its destination."
    }
  }
}

resource "aws_cloudwatch_metric_alarm" "generation" {
  for_each = var.monitoring_enabled ? local.lambda_alarm_metrics : {}

  alarm_name        = "${var.name}-${each.key}"
  alarm_description = each.value.description

  namespace   = "AWS/Lambda"
  metric_name = each.value.metric_name

  dimensions = {
    FunctionName = aws_lambda_function.generation.function_name
  }

  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  datapoints_to_alarm = 1

  comparison_operator = "GreaterThanOrEqualToThreshold"
  threshold           = 1
  treat_missing_data  = "notBreaching"

  alarm_actions = var.alarm_action_arns
  ok_actions    = var.alarm_action_arns

  tags = var.tags
}

resource "aws_cloudwatch_metric_alarm" "failure_queue" {
  count = var.monitoring_enabled ? 1 : 0

  alarm_name        = "${var.name}-failed-events-waiting"
  alarm_description = "Generation failure records are waiting for investigation."

  namespace   = "AWS/SQS"
  metric_name = "ApproximateNumberOfMessagesVisible"

  dimensions = {
    QueueName = aws_sqs_queue.failures.name
  }

  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 1
  datapoints_to_alarm = 1

  comparison_operator = "GreaterThanOrEqualToThreshold"
  threshold           = 1
  treat_missing_data  = "notBreaching"

  alarm_actions = var.alarm_action_arns
  ok_actions    = var.alarm_action_arns

  tags = var.tags
}
