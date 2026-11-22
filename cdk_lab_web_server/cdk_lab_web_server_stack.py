import os.path

from aws_cdk.aws_s3_assets import Asset as S3asset

from aws_cdk import (
    Duration,
    CfnOutput,
    RemovalPolicy,
    Stack,
    aws_ec2 as ec2,
    aws_iam as iam,
    aws_rds as rds,
    aws_elasticloadbalancingv2 as elbv2,
    aws_elasticloadbalancingv2_targets as elbv2_targets,
    # aws_sqs as sqs,
)

from constructs import Construct

dirname = os.path.dirname(__file__)
        
class CdkLabWebServerStack(Stack):

    def __init__(self, scope: Construct, construct_id: str, cdk_lab_vpc: ec2.Vpc, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # The code that defines your stack goes here

        # Instance Role and SSM Managed Policy
        InstanceRole = iam.Role(self, "InstanceSSM", assumed_by=iam.ServicePrincipal("ec2.amazonaws.com"))

        InstanceRole.add_managed_policy(iam.ManagedPolicy.from_aws_managed_policy_name("AmazonSSMManagedInstanceCore"))
        
        # Both web servers share this group; RDS will allow it as a source.
        web_server_security_group = ec2.SecurityGroup(
            self,
            "WebServerSecurityGroup",
            vpc=cdk_lab_vpc,
            description="Shared security group for the web servers",
        )
        web_server_security_group.add_ingress_rule(
            ec2.Peer.any_ipv4(),
            ec2.Port.tcp(80),
            "Allow HTTP from anywhere",
        )

        # Only members of the web-server group may connect to MySQL.
        database_security_group = ec2.SecurityGroup(
            self,
            "DatabaseSecurityGroup",
            vpc=cdk_lab_vpc,
            description="MySQL access from the web servers only",
        )
        database_security_group.add_ingress_rule(
            web_server_security_group,
            ec2.Port.tcp(3306),
            "Allow MySQL from the web servers",
        )

        # RDS may use either of the two isolated private subnets.
        database_subnet_group = rds.SubnetGroup(
            self,
            "DatabaseSubnetGroup",
            description="Private subnets for the MySQL database",
            vpc=cdk_lab_vpc,
            vpc_subnets=ec2.SubnetSelection(
                subnet_type=ec2.SubnetType.PRIVATE_ISOLATED,
            ),
        )

        # Temporary lab database: private, encrypted, and deleted on cleanup.
        database = rds.DatabaseInstance(
            self,
            "MySqlDatabase",
            engine=rds.DatabaseInstanceEngine.MYSQL,
            instance_type=ec2.InstanceType.of(
                ec2.InstanceClass.BURSTABLE4_GRAVITON,
                ec2.InstanceSize.MICRO,
            ),
            vpc=cdk_lab_vpc,
            subnet_group=database_subnet_group,
            security_groups=[database_security_group],
            publicly_accessible=False,
            port=3306,
            credentials=rds.Credentials.from_generated_secret("labadmin"),
            allocated_storage=20,  # Minimum RDS MySQL general-purpose storage.
            storage_type=rds.StorageType.GP3,
            storage_encrypted=True,
            multi_az=False,
            backup_retention=Duration.days(0),
            delete_automated_backups=True,
            deletion_protection=False,
            removal_policy=RemovalPolicy.DESTROY,
        )
        # Delete the generated credentials with this temporary lab as well.
        if database.secret is not None:
            database.secret.apply_removal_policy(RemovalPolicy.DESTROY)

        # Package the existing script once; both servers use it.
        webinitscriptasset = S3asset(
            self,
            "Asset",
            path=os.path.join(dirname, "configure.sh"),
        )

        # Create and configure one server in each public subnet.
        web_servers = []
        for index, subnet in enumerate(cdk_lab_vpc.public_subnets, start=1):
            cdk_lab_web_instance = ec2.Instance(
                self,
                f"cdk_lab_web_instance_{index}",
                vpc=cdk_lab_vpc,
                vpc_subnets=ec2.SubnetSelection(subnets=[subnet]),
                security_group=web_server_security_group,
                instance_type=ec2.InstanceType("t2.micro"),
                machine_image=ec2.MachineImage.latest_amazon_linux2023(),
                role=InstanceRole,
            )

            # Add a startup command to download the script.
            web_servers.append(cdk_lab_web_instance)
            asset_path = (
                cdk_lab_web_instance.user_data.add_s3_download_command(
                    bucket=webinitscriptasset.bucket,
                    bucket_key=webinitscriptasset.s3_object_key,
                )
            )

            # Add a startup command to execute the downloaded script.
            cdk_lab_web_instance.user_data.add_execute_file_command(
                file_path=asset_path,
            )

            # Let this server read the script from S3.
            webinitscriptasset.grant_read(cdk_lab_web_instance.role)

        # Receive public HTTP requests and forward them to the web servers.
        load_balancer = elbv2.ApplicationLoadBalancer(
            self,
            "WebLoadBalancer",
            vpc=cdk_lab_vpc,
            internet_facing=True,
            vpc_subnets=ec2.SubnetSelection(
                subnet_type=ec2.SubnetType.PUBLIC,
            ),
        )
        # InstanceTarget registration does not configure target connections.
        load_balancer.connections.allow_to(
            web_server_security_group,
            ec2.Port.tcp(80),
            "Allow HTTP to the web servers",
        )
        listener = load_balancer.add_listener(
            "HttpListener",
            port=80,
            open=True,
        )
        listener.add_targets(
            "WebServers",
            port=80,
            targets=[elbv2_targets.InstanceTarget(server) for server in web_servers],
            health_check=elbv2.HealthCheck(
                path="/",
                healthy_http_codes="200",
            ),
        )
        CfnOutput(
            self,
            "WebsiteUrl",
            value=f"http://{load_balancer.load_balancer_dns_name}",
            description="Open this URL to access the load-balanced website",
        )

