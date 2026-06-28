locals {
  # Non-secret env shared by every task (the ConfigMap analogue). Secrets are
  # injected separately via `secrets` from Secrets Manager (var.secret_arns).
  common_env = [
    { name = "APP_ENV", value = var.app_env },
    { name = "S3_BUCKET", value = var.s3_bucket },
    { name = "S3_REGION", value = var.s3_region },
    { name = "DASHSCOPE_BASE_URL", value = var.dashscope_base_url },
    { name = "KINORA_LIVE_VIDEO", value = var.kinora_live_video ? "true" : "false" },
    { name = "VIDEO_MODEL", value = var.video_model },
    { name = "VIDEO_MODEL_I2V", value = var.video_model_i2v },
    { name = "VIDEO_MODEL_R2V", value = var.video_model_r2v },
    { name = "CORS_ORIGINS", value = join(",", var.cors_origins) },
  ]

  task_secrets = [for k, v in var.secret_arns : { name = k, valueFrom = v }]
}

resource "aws_ecs_cluster" "this" {
  name = "${var.name}-cluster"
  setting {
    name  = "containerInsights"
    value = "enabled"
  }
  tags = var.tags
}

resource "aws_cloudwatch_log_group" "this" {
  name              = "/kinora/${var.name}"
  retention_in_days = var.log_retention_days
  tags              = var.tags
}

# Internal service discovery namespace so api/workers can reach mcp by DNS.
resource "aws_service_discovery_private_dns_namespace" "this" {
  name        = "${var.name}.kinora.internal"
  vpc         = var.vpc_id
  description = "Kinora internal service discovery"
}

resource "aws_service_discovery_service" "mcp" {
  name = "mcp"
  dns_config {
    namespace_id = aws_service_discovery_private_dns_namespace.this.id
    dns_records {
      type = "A"
      ttl  = 10
    }
    routing_policy = "MULTIVALUE"
  }
  health_check_custom_config {}
}

# ---------------------------------------------------------------------------- #
# Task definitions — one per role, same image, different command.
# ---------------------------------------------------------------------------- #

# Helper local producing a container definition for a backend role.
locals {
  backend_roles = {
    api = {
      command = ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
      cpu     = var.api_cpu
      memory  = var.api_memory
      port    = 8000
      hc_path = "/health"
    }
    mcp = {
      command = ["python", "-m", "app.mcp.run", "--http", "--host", "0.0.0.0", "--port", "8765"]
      cpu     = var.api_cpu
      memory  = var.api_memory
      port    = 8765
      hc_path = ""
    }
    render-worker = {
      command = ["python", "-m", "app.queue.worker"]
      cpu     = var.worker_cpu
      memory  = var.worker_memory
      port    = 0
      hc_path = ""
    }
    ingest-worker = {
      command = ["python", "-m", "app.ingest.recovery"]
      cpu     = var.worker_cpu
      memory  = var.worker_memory
      port    = 0
      hc_path = ""
    }
  }
}

resource "aws_ecs_task_definition" "backend" {
  for_each = local.backend_roles

  family                   = "${var.name}-${each.key}"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = each.value.cpu
  memory                   = each.value.memory
  execution_role_arn       = var.task_execution_role_arn
  task_role_arn            = var.task_role_arn

  container_definitions = jsonencode([
    {
      name         = each.key
      image        = var.container_image
      command      = each.value.command
      essential    = true
      environment  = local.common_env
      secrets      = local.task_secrets
      portMappings = each.value.port > 0 ? [{ containerPort = each.value.port, protocol = "tcp" }] : []
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.this.name
          "awslogs-region"        = var.region
          "awslogs-stream-prefix" = each.key
        }
      }
    }
  ])

  tags = merge(var.tags, { Role = each.key })
}

# Frontend: Nginx over the built Vite renderer (its own image, no secrets/env).
resource "aws_ecs_task_definition" "frontend" {
  family                   = "${var.name}-frontend"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 256
  memory                   = 512
  execution_role_arn       = var.task_execution_role_arn
  task_role_arn            = var.task_role_arn

  container_definitions = jsonencode([
    {
      name         = "frontend"
      image        = var.frontend_container_image
      essential    = true
      portMappings = [{ containerPort = 80, protocol = "tcp" }]
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.this.name
          "awslogs-region"        = var.region
          "awslogs-stream-prefix" = "frontend"
        }
      }
    }
  ])

  tags = merge(var.tags, { Role = "frontend" })
}

# ---------------------------------------------------------------------------- #
# ALB — public entrypoint for api (default) + frontend (/ via host or path).
# ---------------------------------------------------------------------------- #

resource "aws_lb" "this" {
  name               = "${var.name}-alb"
  load_balancer_type = "application"
  internal           = false
  security_groups    = [var.alb_sg_id]
  subnets            = var.public_subnet_ids
  tags               = var.tags
}

resource "aws_lb_target_group" "api" {
  name        = "${var.name}-api-tg"
  port        = 8000
  protocol    = "HTTP"
  vpc_id      = var.vpc_id
  target_type = "ip"
  health_check {
    path                = "/health"
    healthy_threshold   = 2
    unhealthy_threshold = 3
    interval            = 15
    timeout             = 5
    matcher             = "200"
  }
  tags = var.tags
}

resource "aws_lb_target_group" "frontend" {
  name        = "${var.name}-fe-tg"
  port        = 80
  protocol    = "HTTP"
  vpc_id      = var.vpc_id
  target_type = "ip"
  health_check {
    path                = "/"
    healthy_threshold   = 2
    unhealthy_threshold = 3
    interval            = 15
    timeout             = 5
    matcher             = "200"
  }
  tags = var.tags
}

resource "aws_lb_listener" "http" {
  load_balancer_arn = aws_lb.this.arn
  port              = 80
  protocol          = "HTTP"

  # Default to the frontend; route /api*, /health, /sessions, /books, /me to the API.
  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.frontend.arn
  }
}

resource "aws_lb_listener_rule" "api" {
  listener_arn = aws_lb_listener.http.arn
  priority     = 10
  action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.api.arn
  }
  condition {
    path_pattern {
      values = ["/health", "/auth/*", "/books/*", "/sessions/*", "/me/*", "/api/*", "/metrics", "/docs", "/openapi.json"]
    }
  }
}

# ---------------------------------------------------------------------------- #
# Services
# ---------------------------------------------------------------------------- #

resource "aws_ecs_service" "api" {
  name            = "${var.name}-api"
  cluster         = aws_ecs_cluster.this.id
  task_definition = aws_ecs_task_definition.backend["api"].arn
  desired_count   = var.api_desired_count
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = var.private_subnet_ids
    security_groups  = [var.app_sg_id]
    assign_public_ip = false
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.api.arn
    container_name   = "api"
    container_port   = 8000
  }

  depends_on = [aws_lb_listener.http]
  tags       = var.tags
}

resource "aws_ecs_service" "frontend" {
  name            = "${var.name}-frontend"
  cluster         = aws_ecs_cluster.this.id
  task_definition = aws_ecs_task_definition.frontend.arn
  desired_count   = 1
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = var.private_subnet_ids
    security_groups  = [var.app_sg_id]
    assign_public_ip = false
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.frontend.arn
    container_name   = "frontend"
    container_port   = 80
  }

  depends_on = [aws_lb_listener.http]
  tags       = var.tags
}

# MCP: internal-only, registered in Cloud Map; never on the ALB.
resource "aws_ecs_service" "mcp" {
  name            = "${var.name}-mcp"
  cluster         = aws_ecs_cluster.this.id
  task_definition = aws_ecs_task_definition.backend["mcp"].arn
  desired_count   = 1
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = var.private_subnet_ids
    security_groups  = [var.app_sg_id]
    assign_public_ip = false
  }

  service_registries {
    registry_arn = aws_service_discovery_service.mcp.arn
  }

  tags = var.tags
}

resource "aws_ecs_service" "render_worker" {
  name            = "${var.name}-render-worker"
  cluster         = aws_ecs_cluster.this.id
  task_definition = aws_ecs_task_definition.backend["render-worker"].arn
  desired_count   = var.render_worker_desired_count
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = var.private_subnet_ids
    security_groups  = [var.app_sg_id]
    assign_public_ip = false
  }

  tags = var.tags
}

resource "aws_ecs_service" "ingest_worker" {
  name            = "${var.name}-ingest-worker"
  cluster         = aws_ecs_cluster.this.id
  task_definition = aws_ecs_task_definition.backend["ingest-worker"].arn
  desired_count   = var.ingest_worker_desired_count
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = var.private_subnet_ids
    security_groups  = [var.app_sg_id]
    assign_public_ip = false
  }

  tags = var.tags
}

# ---------------------------------------------------------------------------- #
# Autoscaling — api on CPU, render-worker on CPU (a Redis-queue-depth target can
# be layered via a custom CloudWatch metric + a step policy later).
# ---------------------------------------------------------------------------- #

resource "aws_appautoscaling_target" "api" {
  max_capacity       = 6
  min_capacity       = var.api_desired_count
  resource_id        = "service/${aws_ecs_cluster.this.name}/${aws_ecs_service.api.name}"
  scalable_dimension = "ecs:service:DesiredCount"
  service_namespace  = "ecs"
}

resource "aws_appautoscaling_policy" "api_cpu" {
  name               = "${var.name}-api-cpu"
  policy_type        = "TargetTrackingScaling"
  resource_id        = aws_appautoscaling_target.api.resource_id
  scalable_dimension = aws_appautoscaling_target.api.scalable_dimension
  service_namespace  = aws_appautoscaling_target.api.service_namespace

  target_tracking_scaling_policy_configuration {
    predefined_metric_specification {
      predefined_metric_type = "ECSServiceAverageCPUUtilization"
    }
    target_value       = 65
    scale_in_cooldown  = 120
    scale_out_cooldown = 60
  }
}

resource "aws_appautoscaling_target" "render_worker" {
  max_capacity       = 8
  min_capacity       = var.render_worker_desired_count
  resource_id        = "service/${aws_ecs_cluster.this.name}/${aws_ecs_service.render_worker.name}"
  scalable_dimension = "ecs:service:DesiredCount"
  service_namespace  = "ecs"
}

resource "aws_appautoscaling_policy" "render_worker_cpu" {
  name               = "${var.name}-render-cpu"
  policy_type        = "TargetTrackingScaling"
  resource_id        = aws_appautoscaling_target.render_worker.resource_id
  scalable_dimension = aws_appautoscaling_target.render_worker.scalable_dimension
  service_namespace  = aws_appautoscaling_target.render_worker.service_namespace

  target_tracking_scaling_policy_configuration {
    predefined_metric_specification {
      predefined_metric_type = "ECSServiceAverageCPUUtilization"
    }
    target_value       = 70
    scale_in_cooldown  = 180
    scale_out_cooldown = 60
  }
}

output "alb_dns_name" {
  value = aws_lb.this.dns_name
}

output "cluster_name" {
  value = aws_ecs_cluster.this.name
}

output "mcp_service_dns" {
  value = "mcp.${aws_service_discovery_private_dns_namespace.this.name}"
}
