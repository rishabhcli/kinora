output "vpc_id" {
  value = module.network.vpc_id
}

output "vswitch_ids" {
  value = module.network.vswitch_ids
}

output "oss_bucket" {
  value = module.storage.bucket
}

output "oss_s3_endpoint" {
  value = local.oss_s3_endpoint
}

output "rds_connection_string" {
  value = module.database.connection_string
}

output "rds_port" {
  value = module.database.port
}

output "redis_connection_domain" {
  value = module.redis.connection_domain
}

output "redis_port" {
  value = module.redis.port
}

output "api_public_ip" {
  value = module.compute.api_public_ip
}

output "frontend_public_ip" {
  value = module.compute.frontend_public_ip
}

output "mcp_private_ip" {
  value = module.compute.mcp_private_ip
}

output "render_worker_private_ips" {
  value = module.compute.render_worker_private_ips
}

output "ingest_worker_private_ips" {
  value = module.compute.ingest_worker_private_ips
}

output "database_url" {
  value     = local.database_url
  sensitive = true
}

output "redis_url" {
  value     = local.redis_url
  sensitive = true
}

output "jwt_secret" {
  description = "Read with: terraform output -raw jwt_secret"
  value       = module.secrets.jwt_secret
  sensitive   = true
}

output "mcp_auth_token" {
  description = "Read with: terraform output -raw mcp_auth_token"
  value       = module.secrets.mcp_auth_token
  sensitive   = true
}

output "log_project" {
  value = module.observability.log_project
}

output "next_steps" {
  value = join("\n", [
    "1. Build + push the backend image to var.container_image (ACR).",
    "2. Build + push the renderer image to var.frontend_container_image with VITE_KINORA_API_URL=http://<api_public_ip>:8000.",
    "3. SSH to the API node and confirm the container is up: curl http://localhost:8000/health",
    "4. Run migrations once: docker exec kinora-api alembic -c alembic.ini upgrade head (also runs CREATE EXTENSION vector).",
    "5. Seed the demo book: python backend/scripts/seed_demo.py --via api --api-url http://<api_public_ip>:8000",
    "6. Run provider proof: docker exec kinora-api python scripts/provider_preflight.py --json",
    "7. Flip var.kinora_live_video = true only when you intend to spend Wan video-seconds.",
  ])
}
