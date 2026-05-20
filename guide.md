# AWS Console Guide — Logistics Demo (ASG + RDS + Flask-Admin)

Step-by-step ClickOps walkthrough for building the full stack through the
AWS Management Console. Follow the sections in order — each one depends on
the previous.

**Assumed starting point:** A VPC already exists with:
- Two **public** subnets (for the ALB and NAT Gateway)
- Two **private** subnets (for EC2 instances / ASG)
- A private route table associated with the private subnets
- Two **DB** subnets (for RDS, no internet route) with a dedicated DB route table (local route only)

Deploying `CF/VPC - Cidr + GetAz + OutPuts.yaml` from the CF folder provisions all of the above. If your VPC predates the DB subnet addition, follow **§0** before proceeding.

---

## Overview of what you will create

| # | Service | Resource |
|---|---|---|
| 1 | Secrets Manager | RDS password secret |
| 2 | EC2 → Security Groups | ALB SG, App SG, RDS SG, VPCE SG |
| 3.0 | VPC → NAT Gateway | EIP + NAT Gateway (single-AZ, public subnet A) |
| 3 | VPC → Endpoints | S3 Gateway, Secrets Manager, SSM (×3) |
| 4 | RDS | Subnet group (DB subnets) + PostgreSQL instance |
| 5 | IAM | Instance role + instance profile |
| 6 | S3 | Upload the app artifact |
| 7 | EC2 → Load Balancers | ALB + Target Group + HTTP Listener |
| 8 | EC2 → Launch Templates | App launch template |
| 9 | EC2 → Auto Scaling | Auto Scaling Group |
| 10 | CloudWatch | Scale-out + scale-in alarms |
| 11 | Systems Manager | Run schema.sql on an instance |

> **Schema enforces 5 shipment statuses:** `pending`, `in_transit`,
> `delivered`, `delayed`, `canceled`. If you edit `schema.sql`, keep
> `app.py`'s `form_choices` in sync.

---

## 0. Add DB subnets to an existing VPC (skip if using the CF stack)

If your VPC was deployed from `CF/VPC - Cidr + GetAz + OutPuts.yaml` after the
DB subnet update, these subnets already exist — skip to §1.

If you have an older VPC with only public + private subnets, add the DB tier
manually before proceeding.

### 0a. Create DB Subnet A

> **Console:** VPC → Virtual Private Cloud → **Subnets** → Create subnet

1. **VPC ID:** select your VPC
2. **Subnet name:** `<VpcName>-DbSubnet1-<az>-<cidr>` (e.g. `VPC1-DbSubnet1-us-east-1a-10.0.5.0/24`)
3. **Availability Zone:** first AZ (e.g. `us-east-1a`)
4. **IPv4 CIDR block:** a free /24 not already in use (e.g. `10.0.5.0/24`)
5. Click **Create subnet**
6. **Tag:** `Name` = your chosen name; `ManagedByCloudFormation` = `true`

---

### 0b. Create DB Subnet B

Repeat §0a for the second AZ:
- **Availability Zone:** second AZ (e.g. `us-east-1b`)
- **IPv4 CIDR block:** next free /24 (e.g. `10.0.6.0/24`)

---

### 0c. Create the DB route table

> **Console:** VPC → Virtual Private Cloud → **Route Tables** → Create route table

1. **Name:** `<VpcName>-DbRouteTable`
2. **VPC:** select your VPC
3. Click **Create route table**
4. **Do NOT add any additional routes** — leave only the implicit local route.
   This keeps the DB tier fully isolated: no NAT, no IGW.

---

### 0d. Associate both DB subnets to the DB route table

> **Console:** select `<VpcName>-DbRouteTable` → **Subnet associations** tab → **Edit subnet associations**

1. Tick **DB Subnet A** and **DB Subnet B**
2. Click **Save associations**

---

## 1. Secrets Manager — Create the DB secret

> **Console:** AWS → Secrets Manager → **Store a new secret**

1. **Secret type:** Other type of secret
2. **Key/value pairs — add these four rows:**

   | Key | Value |
   |---|---|
   | `username` | `appadmin` |
   | `engine` | `postgres` |
   | `dbname` | `logistics` |
   | `port` | `5432` |

   Leave `password` blank for now — we will let Secrets Manager generate one.
   Actually: switch to **Plaintext** tab and paste the full JSON:

   ```json
   {"username":"appadmin","engine":"postgres","dbname":"logistics","port":5432}
   ```

3. **Encryption key:** aws/secretsmanager (default)
4. Click **Next**
5. **Secret name:** `rds/flask-demo`
6. **Description:** `RDS credentials for the logistics Flask-Admin demo`
7. Click **Next** → **Next** → **Store**

> You will come back to add `host` after RDS is created. The app reads all
> fields at startup — until `host` exists the app will crash, which is fine
> because RDS isn't ready yet either.

---

## 2. Security Groups

> **Console:** EC2 → Network & Security → **Security Groups**

Create three security groups in your VPC, in this order.

---

### 2a. ALB Security Group

1. Click **Create security group**
2. **Security group name:** `logistics-demo-alb-sg`
3. **Description:** `ALB public HTTP ingress`
4. **VPC:** select your VPC
5. **Inbound rules → Add rule:**
   - Type: `HTTP` | Port: `80` | Source: `0.0.0.0/0`
   *(or lock to your classroom IP: `x.x.x.x/32`)*
6. **Outbound rules:** leave the default (all traffic)
7. **Tags → Add tag:** `Name` = `logistics-demo-alb-sg`
8. Click **Create security group**

---

### 2b. App Security Group

1. Click **Create security group**
2. **Security group name:** `logistics-demo-app-sg`
3. **Description:** `App EC2 instances — HTTP from ALB only`
4. **VPC:** select your VPC
5. **Inbound rules → Add rule:**
   - Type: `HTTP` | Port: `80` | Source: *select the ALB SG you just created*
6. **Tags → Add tag:** `Name` = `logistics-demo-app-sg`
7. Click **Create security group**

---

### 2c. RDS Security Group

1. Click **Create security group**
2. **Security group name:** `logistics-demo-rds-sg`
3. **Description:** `RDS PostgreSQL — port 5432 from app instances`
4. **VPC:** select your VPC
5. **Inbound rules → Add two rules:**
   - Type: `PostgreSQL` | Port: `5432` | Source: *App SG* (`logistics-demo-app-sg`)
   - Type: `PostgreSQL` | Port: `5432` | Source: *any existing bastion SG* (if you have one; skip if not)
6. **Tags → Add tag:** `Name` = `logistics-demo-rds-sg`
7. Click **Create security group**

---

### 2d. VPC Endpoint Security Group

This SG is attached to the Interface-type VPC endpoints (Secrets Manager, SSM).

1. Click **Create security group**
2. **Security group name:** `logistics-demo-vpce-sg`
3. **Description:** `Interface VPC endpoints — HTTPS from app instances`
4. **VPC:** select your VPC
5. **Inbound rules → Add rule:**
   - Type: `HTTPS` | Port: `443` | Source: *App SG* (`logistics-demo-app-sg`)
6. **Tags → Add tag:** `Name` = `logistics-demo-vpce-sg`
7. Click **Create security group**

---

## 3.0 NAT Gateway

> **Console:** VPC → Virtual Private Cloud → **NAT Gateways**

The NAT Gateway sits in a public subnet and provides general internet egress
for the private instances — so `pip install -r requirements.txt` can reach
PyPI at boot time and any `dnf install` extras can be downloaded. Create it
before the VPC endpoints because the private route table edit below must
happen before instances launch.

### 3.0a. Allocate an Elastic IP

> **Console:** EC2 → Network & Security → **Elastic IPs** → Allocate Elastic IP address

1. **Network border group:** leave default (your region)
2. Click **Allocate**
3. **Tag the allocation:** `Name` = `logistics-demo-nat-eip`
4. Note the **Allocation ID** — you will need it momentarily.

---

### 3.0b. Create the NAT Gateway

> **Console:** VPC → Virtual Private Cloud → **NAT Gateways** → Create NAT gateway

1. **Name:** `logistics-demo-nat-gw`
2. **Subnet:** select **public subnet A** (the first public subnet — must be public, i.e., has an internet gateway route)
3. **Connectivity type:** Public
4. **Elastic IP allocation ID:** select the EIP you just allocated
5. Click **Create NAT gateway**

> NAT Gateway status changes from `pending` to `available` in about 1 minute.
> You can continue to the next step while it initialises.

---

### 3.0c. Add the default route to the private route table

> **Console:** VPC → Virtual Private Cloud → **Route Tables**

1. Find the **private** route table (the one associated with your private subnets)
2. Click it → **Routes** tab → **Edit routes**
3. Click **Add route:**
   - Destination: `0.0.0.0/0`
   - Target: **NAT Gateway** → select `logistics-demo-nat-gw`
4. Click **Save changes**

> This is the default route that sends all non-local, non-AWS-endpoint traffic
> from private instances through the NAT Gateway to the internet.

---

## 3. VPC Endpoints

> **Console:** VPC → Virtual Private Cloud → **Endpoints**

> **Shared VPC note:** If another stack in this VPC already created the S3
> Gateway Endpoint or any of the four interface endpoints, skip the
> corresponding sub-steps below. If deploying via CFN, set
> `CreateS3GatewayEndpoint=false` and/or `CreateInterfaceEndpoints=false`.

> **Why keep endpoints when we have a NAT Gateway?** The five endpoints below
> are retained alongside the NAT Gateway for two reasons:
> 1. The S3 Gateway Endpoint (free) keeps AL2023 dnf repo traffic and artifact
>    downloads inside the AWS network — faster, no data-transfer charges.
> 2. The SSM endpoints (Secrets Manager, SSM, SSMMessages, EC2Messages) ensure
>    Session Manager and Run Command work even if the NAT Gateway is degraded or
>    removed. This keeps the "SSH into the VMs" experience reliable inside AWS.

---

### 3a. S3 Gateway Endpoint (free)

1. Click **Create endpoint**
2. **Name:** `logistics-demo-s3-gw`
3. **Service category:** AWS services
4. **Services search:** type `s3` → select `com.amazonaws.<region>.s3` with type **Gateway**
5. **VPC:** select your VPC
6. **Route tables:** tick the **private** route table
7. **Policy:** Full access
8. Click **Create endpoint**

---

### 3b. Secrets Manager Interface Endpoint

1. Click **Create endpoint**
2. **Name:** `logistics-demo-secretsmanager`
3. **Service category:** AWS services
4. **Services search:** `secretsmanager` → select `com.amazonaws.<region>.secretsmanager` (type **Interface**)
5. **VPC:** select your VPC
6. **Subnets:** tick both **private** subnets
7. **Security groups:** select `logistics-demo-vpce-sg`
8. **Policy:** Full access
9. Click **Create endpoint**

---

### 3c. SSM Interface Endpoint

1. Click **Create endpoint**
2. **Name:** `logistics-demo-ssm`
3. **Services search:** `ssm` → select `com.amazonaws.<region>.ssm` (type **Interface**)
4. **VPC:** select your VPC
5. **Subnets:** tick both **private** subnets
6. **Security groups:** select `logistics-demo-vpce-sg`
7. Click **Create endpoint**

---

### 3d. SSMMessages Interface Endpoint

Repeat the same steps as 3c but search for `ssmmessages`:
- **Name:** `logistics-demo-ssmmessages`
- Service: `com.amazonaws.<region>.ssmmessages`

---

### 3e. EC2Messages Interface Endpoint

Repeat the same steps but search for `ec2messages`:
- **Name:** `logistics-demo-ec2messages`
- Service: `com.amazonaws.<region>.ec2messages`

> **Why do you need all three SSM endpoints?**
> `ssm` = control plane. `ssmmessages` = the data channel for Run Command and
> Session Manager. `ec2messages` = the older protocol still used by SSM agent.
> All three are required for Run Command to work from a private subnet.

---

## 4. RDS — Subnet group and PostgreSQL instance

### 4a. DB Subnet Group

> **Console:** RDS → Subnet groups → **Create DB subnet group**

1. **Name:** `logistics-demo-subnet-group`
2. **Description:** `Dedicated DB subnets for logistics demo RDS — local route only`
3. **VPC:** select your VPC
4. **Add subnets:**
   - Select each Availability Zone that has a DB subnet
   - For each AZ, select the **DB** subnet (e.g. `10.0.5.0/24` and `10.0.6.0/24`)
   - Do **not** select the private (app) subnets — those are for EC2, not RDS
5. Click **Create**

---

### 4b. RDS Instance

> **Console:** RDS → Databases → **Create database**

1. **Creation method:** Standard create
2. **Engine type:** PostgreSQL
3. **Engine version:** leave default (latest)
4. **Templates:** Free tier *(or Dev/Test for db.t4g.micro)*
5. **DB instance identifier:** `logistics-demo-pg`
6. **Master username:** `appadmin`
7. **Credentials management:** Self managed
8. **Master password:** generate or enter a strong password *(copy it — you will paste it into Secrets Manager in a moment)*
9. **DB instance class:** `db.t4g.micro` (burstable, 2 vCPU, 1 GiB)
   - If not listed: choose "Burstable classes" → `db.t4g.micro`
10. **Storage:**
    - Storage type: `gp3`
    - Allocated storage: `20` GiB
    - Enable storage autoscaling: **unchecked** (demo)
11. **Availability & durability:** Single DB instance *(not Multi-AZ)*
12. **Connectivity:**
    - VPC: select your VPC
    - DB subnet group: `logistics-demo-subnet-group`
    - Public access: **No**
    - VPC security group: remove default → add `logistics-demo-rds-sg`
    - Availability Zone: no preference
13. **Database authentication:** Password authentication
14. **Additional configuration → Initial database name:** `logistics`
15. **Backup retention:** 1 day (demo)
16. **Encryption:** Enable encryption (default KMS key is fine)
17. Click **Create database**

> RDS takes ~5 minutes to become available.

---

### 4c. Update the Secrets Manager secret with the RDS endpoint

Once the RDS instance is **Available**:

1. Go to RDS → Databases → click `logistics-demo-pg`
2. Copy the **Endpoint** (looks like `logistics-demo-pg.xxxx.us-east-1.rds.amazonaws.com`)
3. Go to Secrets Manager → `rds/flask-demo` → **Retrieve secret value** → **Edit**
4. Switch to **Plaintext** and replace the value with the full JSON:

   ```json
   {
     "username": "appadmin",
     "password": "<your-password>",
     "engine": "postgres",
     "dbname": "logistics",
     "port": 5432,
     "host": "<rds-endpoint>"
   }
   ```

5. Click **Save**

---

## 5. IAM — Instance role

> **Console:** IAM → Roles → **Create role**

1. **Trusted entity type:** AWS service
2. **Use case:** EC2
3. Click **Next**
4. **Add permissions — attach these policies:**
   - Search and add: `AmazonSSMManagedInstanceCore`
5. Click **Next**
6. **Role name:** `logistics-demo-instance-role`
7. Click **Create role**

Now add the inline policy for Secrets Manager and S3:

8. Click on `logistics-demo-instance-role` → **Add permissions** → **Create inline policy**
9. Click the **JSON** tab and paste:

   ```json
   {
     "Version": "2012-10-17",
     "Statement": [
       {
         "Effect": "Allow",
         "Action": "secretsmanager:GetSecretValue",
         "Resource": "arn:aws:secretsmanager:*:*:secret:rds/flask-demo*"
       },
       {
         "Effect": "Allow",
         "Action": "s3:GetObject",
         "Resource": "arn:aws:s3:::<your-bucket>/flask-admin-app/app.zip"
       }
     ]
   }
   ```

   Replace `<your-bucket>` with your actual bucket name.

10. **Policy name:** `logistics-demo-app-policy`
11. Click **Create policy**

---

## 6. S3 — Upload the artifact

The artifact is just the application files — no wheel bundling needed. Instances
install Python dependencies at boot from PyPI over the NAT Gateway.

> **`requirements.txt` MUST pin `WTForms==3.0.1`.** Flask-Admin 1.6.1 is
> incompatible with WTForms 3.1+. Removing the pin causes every Create form
> to return HTTP 500. See the troubleshooting appendix.

> **Console:** S3 → your bucket → Create folder `flask-admin-app` → Upload

```bash
# Build locally:
cd /path/to/project
zip app.zip app.py requirements.txt schema.sql
aws s3 cp app.zip s3://<your-bucket>/flask-admin-app/app.zip
```

Or via the console:

1. Navigate to your S3 bucket
2. Click **Create folder** → name it `flask-admin-app` → **Create folder**
3. Open the `flask-admin-app` folder
4. Click **Upload** → **Add files** → select `app.zip`
5. Click **Upload**

---

## 7. Load Balancer

### 7a. Target Group

> **Console:** EC2 → Load Balancing → **Target Groups** → Create target group

1. **Target type:** Instances
2. **Target group name:** `logistics-demo-tg`
3. **Protocol:** HTTP | **Port:** 80
4. **VPC:** select your VPC
5. **Health checks:**
   - Protocol: HTTP
   - Path: `/health`
   - Healthy threshold: `2`
   - Unhealthy threshold: `3`
   - Timeout: `5`
   - Interval: `30`
   - Success codes: `200`
6. Click **Next** → **Create target group** (do NOT register targets yet — the ASG will do that)

---

### 7b. Application Load Balancer

> **Console:** EC2 → Load Balancing → **Load Balancers** → Create load balancer → **Application Load Balancer**

1. **Load balancer name:** `logistics-demo-alb`
2. **Scheme:** Internet-facing
3. **IP address type:** IPv4
4. **Network mapping:**
   - VPC: select your VPC
   - Mappings: tick both AZs → for each, select a **public** subnet
5. **Security groups:** remove default → add `logistics-demo-alb-sg`
6. **Listeners and routing:**
   - Protocol: HTTP | Port: 80
   - Default action: Forward to → `logistics-demo-tg`
7. Click **Create load balancer**

---

## 8. Launch Template

> **Console:** EC2 → Instances → **Launch Templates** → Create launch template

1. **Launch template name:** `logistics-demo-lt`
2. **Template version description:** `v1`
3. **Auto Scaling guidance:** check "Provide guidance to help me set up a template..."
4. **AMI:** click **Browse more AMIs** → **AWS managed** tab → search:
   `al2023-ami` → filter by **arm64** architecture → select the latest Amazon Linux 2023 ARM64 AMI
5. **Instance type:** `t4g.small`
6. **Key pair:** select one if you want SSH access, or leave "Don't include"
7. **Network settings:**
   - Subnet: **Don't include in launch template** (ASG will supply it)
   - Security groups: select `logistics-demo-app-sg`
8. **Storage:** leave default (8 GiB gp3)
9. **Advanced details:**
   - IAM instance profile: `logistics-demo-instance-role`
   - Metadata accessible: **Enabled**
   - Metadata version: **V2 only (token required)**
   - Metadata response hop limit: `1`
10. **User data** — paste the entire script below:

```bash
#!/bin/bash
set -euxo pipefail
exec > >(tee /var/log/user-data.log | logger -t user-data -s 2>/dev/console) 2>&1

REGION="us-east-1"
BUCKET="<your-bucket>"
ARTIFACT_KEY="flask-admin-app/app.zip"
SECRET_NAME="rds/flask-demo"
APP_DIR="/opt/app"

# Update packages (S3 Gateway Endpoint for AL2023 repos)
dnf update -y
dnf install -y python3.11 python3.11-pip nginx postgresql15 unzip awscli-2

# Pull and unpack the artifact
mkdir -p "$APP_DIR"
aws s3 cp "s3://${BUCKET}/${ARTIFACT_KEY}" /tmp/app.zip --region "$REGION"
unzip -o /tmp/app.zip -d "$APP_DIR"
rm -f /tmp/app.zip

# Install Python deps from PyPI (NAT Gateway provides internet access)
python3.11 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install --upgrade pip
"$APP_DIR/venv/bin/pip" install -r "$APP_DIR/requirements.txt"

chown -R ec2-user:ec2-user "$APP_DIR"

FLASK_KEY="$(openssl rand -hex 32)"

cat > /etc/systemd/system/flask-admin.service <<EOF
[Unit]
Description=Flask-Admin Logistics RDS Demo
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=ec2-user
Group=ec2-user
WorkingDirectory=$APP_DIR
Environment="DB_SECRET_NAME=$SECRET_NAME"
Environment="AWS_REGION=$REGION"
Environment="FLASK_SECRET_KEY=$FLASK_KEY"
ExecStart=$APP_DIR/venv/bin/gunicorn \
  --workers 2 --bind 127.0.0.1:8000 \
  --access-logfile - --error-logfile - \
  app:app
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

rm -f /etc/nginx/conf.d/default.conf

cat > /etc/nginx/conf.d/flask.conf <<'NGINX_EOF'
server {
    listen 80 default_server;
    server_name _;

    location = /health {
        access_log off;
        proxy_pass http://127.0.0.1:8000/health;
    }

    location / {
        proxy_pass         http://127.0.0.1:8000;
        proxy_set_header   Host              $host;
        proxy_set_header   X-Real-IP         $remote_addr;
        proxy_set_header   X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto $scheme;
        proxy_read_timeout 60s;
    }
}
NGINX_EOF

systemctl daemon-reload
systemctl enable --now flask-admin
systemctl enable --now nginx

echo "user-data finished OK"
```

> Replace `<your-bucket>` and `us-east-1` with your actual values before saving.

11. Click **Create launch template**

---

## 9. Auto Scaling Group

> **Console:** EC2 → Auto Scaling → **Auto Scaling Groups** → Create Auto Scaling group

**Step 1 — Name and launch template**
1. **Name:** `logistics-demo-asg`
2. **Launch template:** `logistics-demo-lt` → version: `Latest`
3. Click **Next**

**Step 2 — Instance launch options**
4. **VPC:** select your VPC
5. **Availability Zones and subnets:** select both **private** subnets
6. Click **Next**

**Step 3 — Advanced options**
7. **Load balancing:** Attach to an existing load balancer
8. **Choose from your load balancer target groups:** `logistics-demo-tg`
9. **VPC Lattice integration:** none
10. **Health checks:**
    - Turn on **Elastic Load Balancing** health checks
    - Health check grace period: `600`
11. **Monitoring:** Enable group metrics collection
12. Click **Next**

**Step 4 — Group size and scaling**
13. **Desired capacity:** `2`
14. **Minimum capacity:** `2`
15. **Maximum capacity:** `4`
16. **Automatic scaling:** leave at "No scaling policies" for now (we add them in §10)
17. Click **Next** → **Next** → **Next**

**Step 5 — Tags**
18. Add tag: `Name` = `logistics-demo-instance` | Propagate to instances: ✓
19. Add tag: `ManagedByCloudFormation` = `true`
20. Click **Next** → **Create Auto Scaling group**

> The ASG launches 2 instances into the private subnets. Each runs user-data
> (~5-7 min cold start). Wait until both ALB target health checks show
> **healthy** before proceeding.

---

## 10. CloudWatch Alarms and Scaling Policies

### 10a. Scale-out policy (+2 instances when CPU > 40%)

> **Console:** EC2 → Auto Scaling → `logistics-demo-asg` → **Automatic scaling** tab → **Create dynamic scaling policy**

1. **Policy type:** Simple scaling
2. **Scaling policy name:** `logistics-demo-scale-out`
3. **CloudWatch alarm:** click **Create a CloudWatch alarm**

   A new tab opens in CloudWatch:
   - **Select metric:** EC2 → By Auto Scaling Group → `logistics-demo-asg` → `CPUUtilization`
   - **Statistic:** Average | **Period:** 1 minute
   - **Conditions:** Greater than | Threshold: `40`
   - Click **Next**
   - **Alarm name:** `logistics-demo-cpu-high`
   - Click **Next** → **Create alarm**

4. Back in the scaling policy panel, refresh and select `logistics-demo-cpu-high`
5. **Take the action:** Add | `2` | capacity units
6. **Cooldown:** `60` seconds
7. Click **Create**

---

### 10b. Scale-in policy (-2 instances when CPU < 20%)

1. **Create dynamic scaling policy** (same tab)
2. **Policy type:** Simple scaling
3. **Scaling policy name:** `logistics-demo-scale-in`
4. **CloudWatch alarm:** click **Create a CloudWatch alarm**

   In CloudWatch:
   - Same metric: `logistics-demo-asg` → `CPUUtilization`
   - **Conditions:** Lower than | Threshold: `20`
   - **Alarm name:** `logistics-demo-cpu-low`

5. Back in the policy: select `logistics-demo-cpu-low`
6. **Take the action:** Remove | `2` | capacity units
7. **Cooldown:** `120` seconds
8. Click **Create**

---

## 11. Bootstrap the schema (SSM Run Command)

> **Console:** AWS Systems Manager → Run Command → **Run command**

> **Prerequisite:** The SSM VPC endpoints must be active (created in §3c–3e)
> and the instances must have been launched AFTER the endpoints were created.

> **Schema note:** The CHECK constraint enforces exactly five status values:
> `pending`, `in_transit`, `delivered`, `delayed`, `canceled`. If you modify
> `schema.sql`, keep `app.py`'s `form_choices` in sync or the dropdowns will
> diverge from what the DB accepts.
> If instances launched before the endpoints, go to the ASG → Instance management
> → **Start instance refresh** first, then come back here once the new instances
> are healthy.

1. **Document:** search for and select `AWS-RunShellScript`
2. **Target:** Manual → enter the instance ID of one healthy app instance
   *(EC2 → Instances → copy the ID of a running `logistics-demo-instance`)*
3. **Commands** — paste the following block (update secret name/region if needed):

```bash

set -euo pipefail

SECRET_ID="rds/flask-demo"
REGION="us-east-1"

SECRET=$(aws secretsmanager get-secret-value \
  --region "$REGION" \
  --secret-id "$SECRET_ID" \
  --query SecretString \
  --output text)

export PGPASSWORD=$(echo "$SECRET" | python3 -c "import json,sys; print(json.load(sys.stdin)['password'])")
DB_HOST=$(echo "$SECRET" | python3 -c "import json,sys; print(json.load(sys.stdin)['host'])")

# schema.sql is already present from app.zip extraction in user-data
psql -h "$DB_HOST" -U appadmin -d logistics -f /opt/app/schema.sql

# Quick verification
psql -h "$DB_HOST" -U appadmin -d logistics -tAc \
"select (select count(*) from customers),(select count(*) from drivers),(select count(*) from shipments);"

```

> **If `/opt/app/schema.sql` is missing:** use the local script file
> `ssm-run-schema-from-artifact.sh` in this repo, or fallback to inline SQL by
> creating `/tmp/schema.sql` first and running `psql -f /tmp/schema.sql`.
> ```bash
> cat > /tmp/schema.sql <<'SQLEOF'
> -- paste schema.sql content here
> SQLEOF
> psql -h "$DB_HOST" -U appadmin -d logistics -f /tmp/schema.sql
> ```

4. **Output options:** leave defaults (output to S3 is optional)
5. Click **Run**
6. Under **Targets and outputs**, click the instance ID → **View output**

A successful run ends with:

```
 customers | drivers | shipments
-----------+---------+-----------
        10 |      10 |        10
(1 row)
```

After the schema loads, `/dashboard` on the ALB URL returns live data.

---

## 12. Verify everything works

```
http://<alb-dns-name>/health     → "ok"
http://<alb-dns-name>/admin/     → Flask-Admin tables (Customers, Drivers, Shipments)
http://<alb-dns-name>/dashboard  → live row counts and shipment summaries
```

The ALB DNS name is on the Load Balancers page → `logistics-demo-alb` → **DNS name**.

> **WTForms verification:** Open `/admin/customer/new/` and submit the form
> with a unique email address. If the form returns HTTP 500 with a tuple/dict
> error, your `requirements.txt` is missing the `WTForms==3.0.1` pin —
> rebuild the artifact and do an instance refresh. See troubleshooting below.

---

## 13. Cleanup

Delete resources in reverse-dependency order to avoid constraint errors:

1. **EC2 → Auto Scaling Groups:** select `logistics-demo-asg` → Delete
2. **EC2 → Load Balancers:** select `logistics-demo-alb` → Delete
3. **EC2 → Target Groups:** select `logistics-demo-tg` → Delete
4. **RDS → Databases:** select `logistics-demo-pg` → Delete *(uncheck final snapshot for demo cleanup)*
5. **RDS → Subnet groups:** select `logistics-demo-subnet-group` → Delete
6. **VPC → Endpoints:** select all 5 `logistics-demo-*` endpoints → Delete
7. **VPC → NAT Gateways:** select `logistics-demo-nat-gw` → Delete *(wait for it to reach Deleted state)*
8. **EC2 → Elastic IPs:** release `logistics-demo-nat-eip`
9. **VPC → Route Tables:** remove the `0.0.0.0/0` → NAT GW route from the private route table (if not auto-removed)
10. **EC2 → Security Groups:** delete `rds-sg`, `app-sg`, `alb-sg`, `vpce-sg`
11. **Secrets Manager:** `rds/flask-demo` → Delete secret *(30-day recovery window applies)*
12. **IAM → Roles:** `logistics-demo-instance-role` → Delete
13. **S3:** empty and optionally delete the bucket
14. **VPC → Subnets (if created manually via §0):** delete DB Subnet A and DB Subnet B
15. **VPC → Route Tables (if created manually via §0):** delete `<VpcName>-DbRouteTable`

---

## Appendix — Troubleshooting console deployments

**Targets stuck "initial" for more than 10 minutes**
Go to EC2 → Instances → select one → **Actions → Monitor and troubleshoot →
Get system log**. Look for errors in the user-data section.

**pip install fails with "Network is unreachable"**
The NAT Gateway default route is missing from the private route table. Go to
VPC → Route Tables → select the private route table → Routes → verify there
is a `0.0.0.0/0` route pointing to the NAT Gateway (`logistics-demo-nat-gw`).

**SSM targets not showing in Run Command**
The SSM agent needs all three endpoints (ssm, ssmmessages, ec2messages) to
be present when the instance boots. If you created endpoints after the
instances launched, do an instance refresh: ASG → Instance management →
**Start instance refresh** → keep defaults → Start.

**nginx not starting / 502 from ALB**
Check that flask-admin is running: in SSM Run Command, run `systemctl status flask-admin`.
If it failed, run `journalctl -u flask-admin -n 50` to see the error.

**psycopg2 connection refused**
The RDS SG must allow TCP 5432 from the App SG. Go to EC2 → Security Groups →
`logistics-demo-rds-sg` → Inbound rules → verify the rule.

**Secret value missing host field**
If the app crashes with a KeyError on `host`, the Secrets Manager value was
not updated after RDS was created. Follow step 4c to add the `host` key.

**Create form returns HTTP 500 with `'tuple' object has no attribute 'items'`**
WTForms 3.1+ is installed. Flask-Admin 1.6.1 is incompatible. Pin
`WTForms==3.0.1` in `requirements.txt`, rebuild and re-upload the artifact,
then start an instance refresh (ASG → Instance management → **Start instance
refresh**). For a fast live patch, run via SSM Run Command on each instance:
```bash
sudo /opt/app/venv/bin/pip install "WTForms==3.0.1" -q && sudo systemctl restart flask-admin
```

**Stack create fails — route already exists for prefix-list, or conflicting DNS domain**
Another stack in this VPC already owns the S3 Gateway Endpoint or one of the
interface endpoints. From the console, skip the corresponding §3 sub-steps. If
deploying via CFN, set `CreateS3GatewayEndpoint=false` and/or
`CreateInterfaceEndpoints=false` in the stack parameters. See Appendix A.9 of
`aws-asg-rds-flask-guide.md` for details.

**CFN deploy fails: `Template format error: 'Description' length is greater than 1024`**
The `Description:` field in `cfn.yaml` exceeds the 1024-byte CloudFormation
limit. Trim it. See Appendix A.8 of `aws-asg-rds-flask-guide.md`.

**Locked-down variant (no NAT Gateway)**
To run without internet egress, see Appendix B of `aws-asg-rds-flask-guide.md`
for the full diff: remove the NAT Gateway + EIP + default route, and switch to
pre-bundled ARM64 wheels in the artifact.
