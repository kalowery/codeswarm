# AWS Swarm Setup

This guide covers:

- what must exist in AWS before launch
- what must exist on the local launch host (where Codeswarm runs)
- what each AWS-related JSON config field means

## 1. AWS-Side Prerequisites

Codeswarm's AWS provider currently **uses existing network resources** and does not create VPC/subnet/security groups for you.

You must have the following in the target region:

- A VPC and subnet suitable for EC2 launches
- A security group that allows SSH access to instances from your launch host (or bastion path)
- An EC2 key pair
- An AMI compatible with your intended SSH user
- IAM permissions for the AWS identity used on the launch host

### Required resource types

- `subnet-...`: existing subnet ID
- `sg-...`: existing security group ID
- `ami-...`: existing AMI ID
- EC2 key pair name: existing keypair in region

### Minimum AWS permissions

At minimum, the launch identity needs permissions for:

- EC2 instances: run/describe/wait/terminate
- EBS volumes: create/describe/attach/detach/delete/wait
- Security group and subnet describe operations
- Key pair describe operations

In API terms, this typically includes actions like:

- `ec2:RunInstances`
- `ec2:DescribeInstances`
- `ec2:TerminateInstances`
- `ec2:CreateVolume`
- `ec2:AttachVolume`
- `ec2:DeleteVolume`
- `ec2:DescribeVolumes`
- `ec2:DescribeSubnets`
- `ec2:DescribeSecurityGroups`
- `ec2:DescribeKeyPairs`
- `ec2:DescribeVpcs`
- waiter-related describe permissions

If you use SSM parameter lookups for AMIs in your own workflows, you may also need `ssm:GetParameters`.

## 2. Launch Host Prerequisites

The launch host is the machine running Codeswarm router/provider code.

### Required software

- `aws` CLI installed and authenticated
- `ssh` and `rsync`
- Python version compatible with Codeswarm runtime

### Required environment

- `OPENAI_API_KEY` must be set on the launch host

The AWS provider forwards this local key into remote hosts during bootstrap and worker launch, and performs Codex login on each host before starting workers.

### Required local files

- Private key file matching your EC2 `key_name`
- Path configured via `cluster.aws.ssh_private_key_path`

Example:

```bash
export OPENAI_API_KEY=sk-...
aws sts get-caller-identity
ls -l ~/.ssh/codeswarm.pem
```

## 3. AWS Config Fields

Example:

```json
"aws": {
  "region": "us-east-1",
  "ami_id": "ami-xxxxxxxx",
  "subnet_id": "subnet-xxxxxxxx",
  "security_group_id": "sg-xxxxxxxx",
  "key_name": "codeswarm-keypair",
  "ssh_user": "ubuntu",
  "ssh_private_key_path": "~/.ssh/codeswarm.pem",
  "instance_type": "c7i.4xlarge",
  "workers_per_node": 2,
  "ebs_volume_size_gb": 250,
  "delete_ebs_on_shutdown": false
}
```

### `region`

- Type: string
- Example: `us-east-1`
- Must match where your AMI/subnet/key pair/security group exist.

### `ami_id`

- Type: string (`ami-...`)
- Existing AMI ID in the configured region.
- Must support your configured `ssh_user`.

### `subnet_id`

- Type: string (`subnet-...`)
- Existing subnet ID. Not created by Codeswarm.
- Must be launchable by your account and compatible with your connectivity path.

### `security_group_id`

- Type: string (`sg-...`)
- Existing security group ID. Not created by Codeswarm.
- Must permit SSH from launch host or via your network path.

### `key_name`

- Type: string
- Existing EC2 key pair name in the configured region.
- Must match the private key material at `ssh_private_key_path`.

### `ssh_user`

- Type: string
- Linux username for SSH into launched AMI.
- Common values:
  - Amazon Linux: `ec2-user`
  - Ubuntu: `ubuntu`

### `ssh_private_key_path`

- Type: string (local file path)
- Private key path on launch host.
- `~` expansion is supported.

### `instance_type`

- Type: string
- Any valid EC2 instance type for your region/AZ/quota.
- Example: `t3.small`, `c7i.4xlarge`.

### `workers_per_node`

- Type: integer (`>= 1`)
- Number of Codeswarm workers started on each compute node.
- Swarm size is split across nodes according to this value.

### `ebs_volume_size_gb`

- Type: integer (GiB)
- Minimum supported by provider: `8`.
- Size of shared EBS workspace volume.

### `delete_ebs_on_shutdown`

- Type: boolean
- `true`: delete shared EBS volume during terminate
- `false`: retain EBS volume after instance termination

## 4. Additional Optional AWS Fields (Supported by Provider)

The AWS provider code also supports optional fields (if present):

- `security_group_ids`: list form of SG IDs
- `ssh_use_private_ip`: use private IP for SSH selection
- `availability_zone`: explicit AZ override for EBS
- `ebs_device_name`: device mapping (default `/dev/sdf`)
- `node_version`, `codex_version`, `beads_version`
- `iam_instance_profile_arn` or `iam_instance_profile_name`
- `tags`: key-value tags to add to instances

Per-launch provider params can also override/select fields like:

- `node_count`
- `workers_per_node`
- `instance_type`
- `ebs_volume_size_gb`
- `delete_ebs_on_shutdown`
- `ebs_volume_type`
- `ebs_iops`, `ebs_throughput` (for `gp3`)

## 5. Behavior Summary

During AWS launch, provider flow is:

1. Launch coordinator + worker EC2 instances
2. Create and attach shared EBS
3. Mount and export shared workspace (NFS)
4. Install tooling and authenticate Codex on hosts
5. Prepare worker directories and start worker processes

During terminate:

1. Terminate all instances for the swarm job
2. Conditionally delete EBS (based on `delete_ebs_on_shutdown`)

