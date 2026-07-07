# ---------------------------------------------------------------------------- #
# Credentials + region
# ---------------------------------------------------------------------------- #

variable "alicloud_access_key" {
  description = "Alibaba Cloud access key id (or set ALICLOUD_ACCESS_KEY)."
  type        = string
  default     = ""
  sensitive   = true
}

variable "alicloud_secret_key" {
  description = "Alibaba Cloud access key secret (or set ALICLOUD_SECRET_KEY)."
  type        = string
  default     = ""
  sensitive   = true
}

variable "region" {
  description = "Alibaba Cloud region. Defaults to Singapore to match DashScope-intl + OSS oss-ap-southeast-1 (kinora.md §12.6)."
  type        = string
  default     = "ap-southeast-1"
}

variable "zones" {
  description = "Availability zones for the VPC vswitches (RDS/Tair/ECS placement)."
  type        = list(string)
  default     = ["ap-southeast-1a", "ap-southeast-1b"]
}

# ---------------------------------------------------------------------------- #
# Naming / tagging
# ---------------------------------------------------------------------------- #

variable "project" {
  description = "Resource name prefix."
  type        = string
  default     = "kinora"
}

variable "environment" {
  description = "Deployment environment label (prod | staging | dev)."
  type        = string
  default     = "prod"
}

variable "tags" {
  description = "Extra tags applied to taggable resources."
  type        = map(string)
  default     = {}
}

# ---------------------------------------------------------------------------- #
# Network
# ---------------------------------------------------------------------------- #

variable "vpc_cidr" {
  description = "CIDR block for the VPC."
  type        = string
  default     = "10.20.0.0/16"
}

variable "vswitch_cidrs" {
  description = "CIDR blocks for the per-zone vswitches (must align with var.zones)."
  type        = list(string)
  default     = ["10.20.1.0/24", "10.20.2.0/24"]
}

# No default: fail closed. The operator MUST set these so a `terraform apply`
# never silently exposes the API/SSH to the whole internet (the old
# admin_cidr="0.0.0.0/0" did exactly that). SSH is split into its own variable
# so app access and shell access aren't the same rule, and the MCP port (8765)
# is NOT controlled here at all — it's locked to intra-VPC in main.tf.
variable "admin_cidr" {
  description = "CIDR allowed to reach the public API (8000) on the app tier. Lock to your frontend/LB or office egress; never 0.0.0.0/0 in prod. No insecure default — you must set it."
  type        = string

  validation {
    condition     = var.admin_cidr != "0.0.0.0/0"
    error_message = "admin_cidr must not be 0.0.0.0/0 (do not expose the API to the entire internet); use a specific CIDR such as your load balancer or office egress."
  }
}

variable "ssh_cidr" {
  description = "CIDR allowed to reach SSH (22) on the app nodes. Ideally a bastion/VPN egress /32. No insecure default — you must set it."
  type        = string

  validation {
    condition     = var.ssh_cidr != "0.0.0.0/0"
    error_message = "ssh_cidr must not be 0.0.0.0/0 (do not expose SSH to the entire internet); use a bastion/VPN /32."
  }
}

# ---------------------------------------------------------------------------- #
# OSS object storage (clips, keyframes, audio, refs, canon vault)
# ---------------------------------------------------------------------------- #

variable "oss_bucket_name" {
  description = "Globally-unique OSS bucket name for Kinora assets."
  type        = string
  default     = "kinora-assets"
}

# ---------------------------------------------------------------------------- #
# ApsaraDB RDS for PostgreSQL (canon graph, episodic pgvector store, jobs)
# ---------------------------------------------------------------------------- #

variable "rds_engine_version" {
  description = "PostgreSQL major version (>= 14 supports the pgvector extension)."
  type        = string
  default     = "16.0"
}

variable "rds_instance_type" {
  description = "RDS instance class (region-specific; e.g. pg.n2.small.1, pg.n4.medium.1)."
  type        = string
  default     = "pg.n2.small.1"
}

variable "rds_instance_storage" {
  description = "RDS data disk size in GB."
  type        = number
  default     = 50
}

variable "rds_storage_type" {
  description = "RDS storage type (cloud_essd | cloud_ssd)."
  type        = string
  default     = "cloud_essd"
}

variable "db_name" {
  description = "Application database name."
  type        = string
  default     = "kinora"
}

variable "db_account_name" {
  description = "Application database account."
  type        = string
  default     = "kinora"
}

variable "db_account_password" {
  description = "DB account password. Leave empty to auto-generate a strong one."
  type        = string
  default     = ""
  sensitive   = true
}

# ---------------------------------------------------------------------------- #
# Tair (Redis) — render queue, scheduler session state, pub/sub fanout
# ---------------------------------------------------------------------------- #

variable "redis_instance_class" {
  description = "Tair/Redis instance class (region-specific; e.g. redis.master.small.default)."
  type        = string
  default     = "redis.master.small.default"
}

variable "redis_engine_version" {
  description = "Redis engine version."
  type        = string
  default     = "7.0"
}

variable "redis_password" {
  description = "Redis AUTH password. Leave empty to auto-generate a strong one."
  type        = string
  default     = ""
  sensitive   = true
}

# ---------------------------------------------------------------------------- #
# ECS compute (api + render-worker + mcp). Alternatively run the workers on
# Function Compute; see README.md.
# ---------------------------------------------------------------------------- #

variable "ecs_instance_type" {
  description = "ECS instance type for the app nodes (region-specific)."
  type        = string
  default     = "ecs.g7.large"
}

variable "ecs_password" {
  description = "Root/login password for the ECS instances (meet Alibaba complexity rules)."
  type        = string
  default     = ""
  sensitive   = true
}

variable "ecs_system_disk_category" {
  description = "ECS system disk category."
  type        = string
  default     = "cloud_essd"
}

variable "ecs_internet_bandwidth_out" {
  description = "Public egress bandwidth (Mbps) for pulling images + reaching DashScope. Set 0 to keep nodes private (then provide a NAT gateway)."
  type        = number
  default     = 10
}

variable "render_worker_count" {
  description = "Number of render-worker ECS nodes (horizontal scale)."
  type        = number
  default     = 1
}

variable "ingest_worker_count" {
  description = "Number of ingest recovery-worker ECS nodes."
  type        = number
  default     = 1
}

# ---------------------------------------------------------------------------- #
# Application runtime
# ---------------------------------------------------------------------------- #

variable "container_image" {
  description = "Fully-qualified Kinora backend image (e.g. registry.ap-southeast-1.aliyuncs.com/kinora/backend:TAG)."
  type        = string
  default     = "registry.ap-southeast-1.aliyuncs.com/kinora/backend:latest"
}

variable "frontend_container_image" {
  description = "Fully-qualified Kinora web renderer image, built from infra/docker/desktop.Dockerfile with VITE_KINORA_API_URL pointed at the API."
  type        = string
  default     = "registry.ap-southeast-1.aliyuncs.com/kinora/frontend:latest"
}

variable "build_images_on_instance" {
  description = "When true, ECS cloud-init clones source_repo_url/source_ref and builds the backend/frontend Docker images locally instead of pulling pre-pushed registry images. Useful for hackathon proof deployments before ACR is provisioned."
  type        = bool
  default     = false
}

variable "source_repo_url" {
  description = "Public Git repository cloned by cloud-init when build_images_on_instance is true."
  type        = string
  default     = "https://github.com/rishabhcli/kinora.git"
}

variable "source_ref" {
  description = "Branch, tag, or commit SHA checked out by cloud-init when build_images_on_instance is true."
  type        = string
  default     = "main"
}

variable "dashscope_api_key" {
  description = "DashScope (Model Studio) intl API key. Injected into the app env; never commit it."
  type        = string
  default     = ""
  sensitive   = true
}

variable "dashscope_base_url" {
  description = "DashScope base URL (intl endpoint)."
  type        = string
  default     = "https://dashscope-intl.aliyuncs.com"
}

variable "kinora_live_video" {
  description = "Go-live gate for real Wan video spend (kinora.md §11.1). Keep false until you intend to spend video-seconds."
  type        = bool
  default     = false
}

variable "video_model" {
  description = "Hosted Wan text-to-video model id (verified available on the intl tier)."
  type        = string
  default     = "wan2.7-t2v"
}

variable "video_model_i2v" {
  description = "Hosted Wan image-to-video model id."
  type        = string
  default     = "wan2.7-i2v"
}

variable "video_model_r2v" {
  description = "Hosted Wan reference-to-video model id (rides the i2v model)."
  type        = string
  default     = "wan2.7-i2v"
}

# ---------------------------------------------------------------------------- #
# Application auth / CORS (the env the app now enforces in non-local)
# ---------------------------------------------------------------------------- #

variable "jwt_secret" {
  description = "JWT signing secret (HMAC) for the API. Leave empty to auto-generate a strong one (like the DB/Redis passwords). Injected as JWT_SECRET; the app refuses to boot in non-local with its insecure built-in default."
  type        = string
  default     = ""
  sensitive   = true
}

variable "mcp_auth_token" {
  description = "Bearer token the MCP HTTP server requires (and its clients send). Leave empty to auto-generate a strong one. Injected as MCP_AUTH_TOKEN."
  type        = string
  default     = ""
  sensitive   = true
}

variable "cors_origins" {
  description = "Browser origin(s) the API allows under credentialed CORS — the deployed frontend origin(s). Rendered to CORS_ORIGINS as a comma-separated list. No insecure default; a wildcard is rejected because allow_credentials cannot be combined with '*'."
  type        = list(string)

  validation {
    condition     = length(var.cors_origins) > 0 && !contains(var.cors_origins, "*")
    error_message = "cors_origins must be a non-empty list of explicit origins (e.g. [\"https://app.example.com\"]); a credentialed wildcard '*' is not allowed."
  }
}
