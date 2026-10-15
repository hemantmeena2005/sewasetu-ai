variable "keypair_name" {
  description = "Name of existing Nova SSH keypair"
  type        = string
}

variable "image_name" {
  description = "Base operating system image name"
  type        = string
  default     = "Ubuntu-22.04-LTS-Jammy"
}

variable "flavor_name" {
  description = "Compute flavor name (minimum 2 vCPU, 4GB RAM)"
  type        = string
  default     = "m1.medium"
}

variable "public_network_name" {
  description = "Public network name for floating IP pool"
  type        = string
  default     = "public"
}

variable "public_network_id" {
  description = "External public network UUID for gateway routing"
  type        = string
}

variable "storage_volume_size_gb" {
  description = "Size of persistent Cinder storage volume in GB"
  type        = number
  default     = 50
}

variable "cinder_volume_type" {
  description = "Cinder volume type (e.g. high-iops, standard, ceph)"
  type        = string
  default     = "high-iops"
}

variable "admin_cidr" {
  description = "CIDR range allowed for administrative SSH access"
  type        = string
  default     = "0.0.0.0/0"
}
