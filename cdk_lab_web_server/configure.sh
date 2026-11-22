#!/bin/bash
set -euo pipefail
# EC2 user data runs as root. Install Apache and a MySQL-compatible client.
dnf install -y httpd mariadb105

# Each server writes its own hostname into the page it serves.
cat > /var/www/html/index.html <<EOF
<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><title>CDK Web Server Lab</title></head>
<body>
  <h1>CDK Web Server Lab</h1>
  <p>Served by: $(hostname)</p>
</body>
</html>
EOF

systemctl start httpd
systemctl enable httpd
