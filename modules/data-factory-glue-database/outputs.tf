output "glue_database_name" {
  description = "The name of the Glue database"
  value       = aws_glue_catalog_database.this.name
}

output "glue_database_arn" {
  description = "The ARN of the Glue database"
  value       = aws_glue_catalog_database.this.arn
}