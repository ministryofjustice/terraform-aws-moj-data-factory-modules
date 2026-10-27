output "table_name" {
  description = "Name of the Ingestion Registry table."
  value       = aws_dynamodb_table.this.name
}

output "table_arn" {
  description = "ARN of the Ingestion Registry table."
  value       = aws_dynamodb_table.this.arn
}
