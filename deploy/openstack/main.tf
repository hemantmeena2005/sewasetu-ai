# Terraform OpenStack Infrastructure Definition for Sewa Setu
# Provider: terraform-provider-openstack/openstack

terraform {
  required_version = ">= 1.3.0"
  required_providers {
    openstack = {
      source  = "terraform-provider-openstack/openstack"
      version = "~> 1.53.0"
    }
  }
}

# 1. Private Network & Subnet
resource "openstack_networking_network_v2" "sewasetu_net" {
  name           = "sewasetu-prod-net"
  admin_state_up = true
}

resource "openstack_networking_subnet_v2" "sewasetu_subnet" {
  name       = "sewasetu-prod-subnet"
  network_id = openstack_networking_network_v2.sewasetu_net.id
  cidr       = "192.168.100.0/24"
  ip_version = 4
  gateway_ip = "192.168.100.1"
  dns_nameservers = [
    "1.1.1.1",
    "8.8.8.8"
  ]
}

# 2. Router to Public Network
resource "openstack_networking_router_v2" "sewasetu_router" {
  name                = "sewasetu-prod-router"
  admin_state_up      = true
  external_network_id = var.public_network_id
}

resource "openstack_networking_router_interface_v2" "sewasetu_router_interface" {
  router_id = openstack_networking_router_v2.sewasetu_router.id
  subnet_id = openstack_networking_subnet_v2.sewasetu_subnet.id
}

# 3. Security Group (Least Privilege)
resource "openstack_networking_secgroup_v2" "sewasetu_secgroup" {
  name        = "sewasetu-prod-secgroup"
  description = "Security rules for Sewa Setu: HTTPS, HTTP, and SSH only"
}

resource "openstack_networking_secgroup_rule_v2" "allow_ssh" {
  direction         = "ingress"
  ethertype         = "IPv4"
  protocol          = "tcp"
  port_range_min    = 22
  port_range_max    = 22
  remote_ip_prefix  = var.admin_cidr
  security_group_id = openstack_networking_secgroup_v2.sewasetu_secgroup.id
}

resource "openstack_networking_secgroup_rule_v2" "allow_http" {
  direction         = "ingress"
  ethertype         = "IPv4"
  protocol          = "tcp"
  port_range_min    = 80
  port_range_max    = 80
  remote_ip_prefix  = "0.0.0.0/0"
  security_group_id = openstack_networking_secgroup_v2.sewasetu_secgroup.id
}

resource "openstack_networking_secgroup_rule_v2" "allow_https" {
  direction         = "ingress"
  ethertype         = "IPv4"
  protocol          = "tcp"
  port_range_min    = 443
  port_range_max    = 443
  remote_ip_prefix  = "0.0.0.0/0"
  security_group_id = openstack_networking_secgroup_v2.sewasetu_secgroup.id
}

# 4. Cinder Persistent Block Storage (for PostgreSQL data and document uploads)
resource "openstack_blockstorage_volume_v3" "sewasetu_storage" {
  name        = "sewasetu-data-volume"
  description = "Persistent Cinder volume for PostgreSQL database and private uploads"
  size        = var.storage_volume_size_gb
  volume_type = var.cinder_volume_type
}

# 5. Network Port & Floating IP
resource "openstack_networking_port_v2" "sewasetu_port" {
  name               = "sewasetu-server-port"
  network_id         = openstack_networking_network_v2.sewasetu_net.id
  admin_state_up     = true
  security_group_ids = [openstack_networking_secgroup_v2.sewasetu_secgroup.id]

  fixed_ip {
    subnet_id = openstack_networking_subnet_v2.sewasetu_subnet.id
  }
}

resource "openstack_networking_floatingip_v2" "sewasetu_fip" {
  pool = var.public_network_name
}

resource "openstack_networking_floatingip_associate_v2" "sewasetu_fip_assoc" {
  floating_ip = openstack_networking_floatingip_v2.sewasetu_fip.address
  port_id     = openstack_networking_port_v2.sewasetu_port.id
}

# 6. Nova Compute Server
resource "openstack_compute_instance_v2" "sewasetu_instance" {
  name        = "sewasetu-prod-app"
  image_name  = var.image_name
  flavor_name = var.flavor_name
  key_pair    = var.keypair_name

  network {
    port = openstack_networking_port_v2.sewasetu_port.id
  }

  user_data = file("${path.module}/cloud-init.yaml")
}

# 7. Volume Attachment
resource "openstack_compute_volume_attach_v2" "sewasetu_vol_attach" {
  instance_id = openstack_compute_instance_v2.sewasetu_instance.id
  volume_id   = openstack_blockstorage_volume_v3.sewasetu_storage.id
  device      = "/dev/vdb"
}

# Outputs
output "public_ip" {
  description = "Public Floating IP assigned to Sewa Setu"
  value       = openstack_networking_floatingip_v2.sewasetu_fip.address
}

output "portal_url" {
  description = "Citizen Pension Portal URL"
  value       = "https://${openstack_networking_floatingip_v2.sewasetu_fip.address}/"
}

output "citizen_mcp_url" {
  description = "Citizen MCP Server Streamable HTTP URL"
  value       = "https://${openstack_networking_floatingip_v2.sewasetu_fip.address}/mcp/citizen"
}

output "officer_mcp_url" {
  description = "Officer MCP Server Streamable HTTP URL"
  value       = "https://${openstack_networking_floatingip_v2.sewasetu_fip.address}/mcp/officer"
}

output "health_check_url" {
  description = "Production Readiness Check Endpoint"
  value       = "https://${openstack_networking_floatingip_v2.sewasetu_fip.address}/readyz"
}
