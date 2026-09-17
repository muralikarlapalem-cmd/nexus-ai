# Network Connectivity Runbook

**Category:** Network | **Owner:** Infrastructure Team | **Version:** 2.1

## Overview

This runbook covers diagnosis and resolution of network connectivity issues
including server unreachability, DNS failures, packet loss, and firewall blocks.

---

## Symptom: Server Unreachable / Ping Timeout

### Prerequisites
- Access to the affected server's console or bastion host
- Network team credentials for firewall portal
- Runbook access: `infra/networking/vpc-config.md`

### Diagnosis Steps

1. **Verify the server is powered on**
   ```bash
   # Check EC2 / VM status
   aws ec2 describe-instance-status --instance-ids <id>
   ```

2. **Test ICMP ping from bastion**
   ```bash
   ping -c 5 <server-ip>
   traceroute <server-ip>
   ```

3. **Check Security Group / Firewall rules**
   - Login to AWS Console → EC2 → Security Groups
   - Verify inbound rules allow traffic on required ports
   - Common missing rules: TCP 22 (SSH), TCP 443 (HTTPS), TCP 5432 (Postgres)

4. **Verify the subnet route table**
   ```bash
   aws ec2 describe-route-tables --filters "Name=association.subnet-id,Values=<subnet-id>"
   ```

5. **Check NACLs (Network ACLs)**
   - NACLs are stateless — verify both inbound AND outbound rules

### Resolution

- **Firewall block**: Add inbound rule in Security Group for source IP/CIDR
- **Route missing**: Add route to Internet Gateway or NAT Gateway
- **NACL deny**: Add allow rule at lower priority than deny
- **Server crashed**: Start the EC2 instance via console or `aws ec2 start-instances`

### Escalation
If unresolved in 30 minutes → escalate to Network Team Lead (Slack: `#network-oncall`)

---

## Symptom: High Packet Loss (> 2%)

### Diagnosis
```bash
mtr --report --report-cycles 100 <destination-ip>
```
Look for packet loss at a specific hop — the problem is upstream of that hop.

### Resolution
- Internal hop loss: Contact cloud provider support
- Last-hop loss: Check server NIC, OS network config (`ip link`, `ethtool eth0`)

---

## Symptom: DNS Resolution Failure

### Diagnosis
```bash
nslookup api.internal.nexus.ai
dig @8.8.8.8 api.internal.nexus.ai
dig @<internal-dns-server> api.internal.nexus.ai
```

### Resolution
1. Check `/etc/resolv.conf` — should point to internal DNS (10.0.0.2)
2. Restart `systemd-resolved`: `sudo systemctl restart systemd-resolved`
3. Flush DNS cache: `sudo resolvectl flush-caches`
4. If internal DNS is down → failover to `8.8.8.8` temporarily and page DNS team

---

## SLA Reference

| Priority | Response Time | Resolution Target |
|----------|--------------|-------------------|
| CRITICAL | 15 min       | 4 hours           |
| HIGH     | 30 min       | 8 hours           |
| MEDIUM   | 2 hours      | 24 hours          |

---

*Last updated: 2025-06-01 | Reviewed by: Infra Lead*
