# Copyright 2008-2018 Univa Corporation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import configparser
import getpass
import json
import os
import shutil
import subprocess
import sys

import boto3
import botocore
import click
import colorama

from tortuga.db.dbManager import DbManager
from tortuga.exceptions.resourceNotFound import ResourceNotFound
from tortuga.resourceAdapterConfiguration.api \
    import ResourceAdapterConfigurationApi


DEFAULT_AWS_REGION = 'us-east-1'

DEFAULT_RESOURCE_ADAPTER_CONFIGURATION_PROFILE_NAME = 'default'


def get_ec2_metadata():
    cmd = '/opt/puppetlabs/bin/facter --json ec2_metadata'

    p = subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE)

    result = json.load(p.stdout)

    retval = p.wait()
    if retval != 0:
        return {}

    return result['ec2_metadata'] if 'ec2_metadata' in result else {}


def disable_colour(ctx, param, value):
    colorama.init(strip=value)


@click.command()
@click.option('--verbose', is_flag=True, default=False,
              help='Enable verbose output')
@click.option('--debug', is_flag=True, default=False,
              help='Enable debug mode')
@click.option('--no-autodetect', is_flag=True, default=False,
              help='Disable AWS region autodetect')
@click.option('--ignore-iam', is_flag=True, default=False,
              help='Ignore (current) IAM profile/assumed-role')
@click.option('--unattended', is_flag=True, default=False,
              help='Run without prompting for input')
@click.option('--region', default=DEFAULT_AWS_REGION,
              help='Override detected AWS region')
@click.option('--no-color', '--no-colour', is_flag=True, expose_value=False,
              callback=disable_colour,
              help='Disable colo[u]r output')
@click.option('--profile',
              help=('Resource adapter configuration profile name'
                    ' (default: {0})'.format(
                        DEFAULT_RESOURCE_ADAPTER_CONFIGURATION_PROFILE_NAME)),
              default=DEFAULT_RESOURCE_ADAPTER_CONFIGURATION_PROFILE_NAME)
def main(verbose, debug, no_autodetect, ignore_iam, unattended, region, profile):
    ec2_metadata = get_ec2_metadata()

    print(bright_white('Configuring Tortuga AWS resource adapter'))

    if unattended:
        no_autodetect = False
        ignore_iam = False
        verbose = True

    if not region:
        region = ec2_metadata['placement']['availability-zone'][:-1] \
            if ec2_metadata and not no_autodetect else None

        if verbose:
            print_statement(
                'Region [{0}] obtained from EC2 instance metadata',
                region)

    if region:
        print_statement('Detected AWS region: [{0}]', region)
    else:
        if not no_autodetect:
            error_message('Error: unable to determine current AWS region')

            if unattended:
                sys.exit(1)

        # Query for region
        prompt = format_string_with_arg('AWS region [{0}]:', region)

        response = input(prompt + ' ')
        if not response:
            region = DEFAULT_AWS_REGION
        else:
            region = response

            try:
                # validate region
                session = boto3.session.Session()
                if region not in session.get_available_regions('ec2'):
                    error_message('Error: invalid AWS region [{0}]', region)

                    sys.exit(1)
            except botocore.exceptions.EndpointConnectionError:
                error_message(
                    'Error connecting to EC2 endpoint (invalid region?)')

                sys.exit(1)

    creds = False

    if not ignore_iam:
        # if IAM profile is not in use, query access/secret keys
        client = boto3.client('sts')

        response = None

        print(colorama.Fore.GREEN + colorama.Style.BRIGHT +
                   'Checking for IAM profile...' +
                   colorama.Style.RESET_ALL, end=' ')

        try:
            response = client.get_caller_identity()

            iam_arn = response['Arn']

            iam_user_policy = iam_arn.split(':')[5]

            assumed_role = False
            if iam_user_policy.startswith('assumed-role/'):
                _, iam_profile_name, _ = iam_user_policy.split('/', 2)
                assumed_role = True
            else:
                _, iam_profile_name = iam_user_policy.split('/', 1)

            print(bright_white(iam_profile_name))

            if not assumed_role:
                print(colorama.Style.BRIGHT + colorama.Fore.YELLOW +
                           '*' + colorama.Style.RESET_ALL +
                           format_string_with_arg(
                               ' Ensure IAM profile [{0}] is used to launch'
                               ' Grid Engine/Tortuga instance',
                               iam_profile_name))

            creds = True
        except botocore.exceptions.NoCredentialsError:
            print(colorama.Fore.YELLOW +
                       colorama.Style.BRIGHT + 'not found' +
                       colorama.Style.RESET_ALL)
        except Exception as exc:
            print(colorama.Style.DIM +
                       '[debug] Error querying IAM profile name:'
                       ' {0}'.format(exc) + colorama.Style.RESET_ALL)
    else:
        if debug:
            print(colorama.Style.DIM +
                       '[debug] Ignoring IAM profile (--ignore-iam argument'
                       ' specified)' + colorama.Style.RESET_ALL)

    access_key = None
    secret_key = None

    if not creds:
        if unattended:
            # cannot query for credentials in unattended mode
            print(
                'Unable to query user for credentials in unattended mode')
            print('Exiting.')

            sys.exit(1)

        if os.getenv('AWS_ACCESS_KEY') and os.getenv('AWS_SECRET_ACCESS_KEY'):
            print(colorama.Fore.GREEN + colorama.Style.BRIGHT +
                       'Using AWS_ACCESS_KEY/AWS_SECRET_ACCESS_KEY from'
                       ' environment' + colorama.Style.RESET_ALL)

            # use envvars for credentials
            access_key = os.getenv('AWS_ACCESS_KEY')

            secret_key = os.getenv('AWS_SECRET_ACCESS_KEY')

        if not access_key:
            print(
                format_string_with_arg(
                    'IAM profile not detected. Using'
                    ' AWS access and secret access keys.',
                    forecolour=colorama.Fore.YELLOW)
            )

            access_key = input(colorama.Style.BRIGHT +
                               'AWS access key: ' + colorama.Style.RESET_ALL)
            if not access_key:
                print('Aborted by user.')
                sys.exit(1)

            secret_key = getpass.getpass(colorama.Style.BRIGHT +
                                         'AWS secret key: ' +
                                         colorama.Style.RESET_ALL)
            if not secret_key:
                print('Aborted by user.')
                sys.exit(1)

        # validate AWS credentials
        print('Validating AWS access credentials...', end=' ')

        ec2 = boto3.client('ec2',
                           region_name=region,
                           aws_access_key_id=access_key,
                           aws_secret_access_key=secret_key)

        try:
            if debug:
                print()
                print(colorama.Style.DIM +
                           '[debug] Calling \'describe_images()\' to '
                           'validate credentials' + colorama.Style.RESET_ALL)

            ec2.describe_images(Owners=['self'])

            print(colorama.Fore.GREEN +
                       colorama.Style.BRIGHT + 'ok.' +
                       colorama.Style.RESET_ALL)
        except botocore.exceptions.ClientError as exc:
            print(colorama.Fore.RED +
                       colorama.Style.BRIGHT + 'failed.' +
                       colorama.Style.RESET_ALL)
            errmsg = 'Error validating provided access credentials'
            if exc.response['Error']['Code'] != 'AuthFailure':
                error_message(errmsg + ': {0}', exc)
            else:
                error_message(errmsg)

            sys.exit(1)

        # Write/update "~/.aws/credentials"
        update_aws_credentials(region, access_key, secret_key)
    else:
        # using available IAM profile
        ec2 = boto3.client('ec2', region_name=region)

    # keypair

    keypair = None

    try:
        # Extract keypair name from ssh public key metadata
        for _, key_values in ec2_metadata['public-keys'].items():
            keypair = key_values['openssh-key'].split(' ')[-1]

            break
    except Exception:
        pass

    while not keypair:
        response = input('Keypair [? for list]: ')
        if not response:
            break

        if response.startswith('?'):
            result = ec2.describe_key_pairs()

            for tmp_keypair in result['KeyPairs']:
                print('    ' + colorama.Fore.YELLOW +
                           colorama.Style.BRIGHT +
                           tmp_keypair['KeyName'] +
                           colorama.Style.RESET_ALL +
                           colorama.Style.DIM +
                           ' (' + tmp_keypair['KeyFingerprint'] +
                           ')' + colorama.Style.RESET_ALL)

            continue

        # validate keypair
        try:
            ec2.describe_key_pairs(KeyNames=[response])

            keypair = response

            break
        except botocore.exceptions.ClientError as exc:
            if exc.response['Error']['Code'] == 'InvalidKeyPair.NotFound':
                print('Keypair invalid or inaccessible.')
                continue

            print('{0}'.format(exc.message))

            continue

    if not keypair:
        print('Aborted by user.')
        sys.exit(1)

    # query default compute instance type
    instance_type = 'm4.large'
    while not instance_type:
        response = input('Default compute instance type [m4.large]: ')
        if not response:
            instance_type = 'm4.large'
            break

        instance_type = response

    # TODO: validate instance type (???)

    # get values from EC2 metadata
    subnet_id = None
    group_id = None
    vpc_id = None

    if ec2_metadata:
        for mac, values in \
                ec2_metadata['network']['interfaces']['macs'].items():
            subnet_id = values['subnet-id']
            group_id = values['security-group-ids']
            vpc_id = values['vpc-id']
            break

    # subnet_id
    subnets = ec2.describe_subnets()
    while not subnet_id:
        response = input('Subnet ID [? for list]: ')
        if not response:
            continue

        if response.startswith('?'):
            for subnet in subnets['Subnets']:
                name = get_resource_name_from_tag(subnet)

                buf = colorama.Fore.YELLOW + colorama.Style.BRIGHT + \
                    subnet['SubnetId'] + colorama.Style.RESET_ALL + \
                    colorama.Style.DIM

                if name:
                    buf += ' ' + name

                buf += ' ({0}) (VPC ID: {1})'.format(subnet['CidrBlock'],
                                                     subnet['VpcId']) + \
                    colorama.Style.RESET_ALL

                print('    ' + buf)

            continue

        for subnet in subnets['Subnets']:
            if subnet['SubnetId'] == response:
                subnet_id = response
                vpc_id = subnet['VpcId']
                break
        else:
            # print('Error: invalid subnet ID')
            error_message('Error: invalid subnet ID')

            continue

        break

    # security group(s)
    while not group_id:
        response = input('Security group ID (? for list): ')
        if not response:
            break

        if response.startswith('?'):
            result = ec2.describe_security_groups(
                Filters=[{'Name': 'vpc-id', 'Values': [vpc_id]}])

            for secgroup in result['SecurityGroups']:
                print('    ' + colorama.Fore.YELLOW +
                           colorama.Style.BRIGHT +
                           secgroup['GroupId'] + ' ' +
                           colorama.Fore.WHITE +
                           colorama.Style.DIM +
                           secgroup['GroupName'] +
                           colorama.Style.RESET_ALL)

            continue

        try:
            result = ec2.describe_security_groups(
                GroupIds=[response],
                Filters=[{'Name': 'vpc-id', 'Values': [vpc_id]}])

            if result['SecurityGroups']:
                group_id = result['SecurityGroups'][0]['GroupId']

                break
        except botocore.exceptions.ClientError:
            pass

        print('Invalid security group ID')

    if not group_id:
        print('Aborted by user.')
        sys.exit(1)

    ami_id = ec2_metadata['ami-id'] if ec2_metadata else None
    while not ami_id:
        response = input('UGE/Tortuga AMI ID: ')
        if not response:
            print('Aborted by user.')

            sys.exit(1)

        try:
            result = ec2.describe_images(ImageIds=[response])
            if result['Images']:
                ami_id = response

                break
        except botocore.exceptions.ClientError as exc:
            if exc.response['Error']['Code'] == 'InvalidAMIID.Malformed':
                print('Malformed AMI ID')

                continue
            elif exc.response['Error']['Code'] == 'InvalidAMIID.NotFound':
                print('AMI not found or inaccessible')

                continue

            print('Invalid AMI ID: {0}'.format(exc.message))

            continue

        break

    adapter_cfg = {
        'allocate_public_ip': 'true',
        'cloud_init_script_template': 'aws_cloud_init.yaml.j2',
    }

    if access_key and secret_key:
        adapter_cfg['awsaccesskey'] = access_key
        adapter_cfg['awssecretkey'] = secret_key

    override_adapter_cfg = {
        'keypair': keypair,
        'ami': ami_id,
        'instancetype': instance_type,
        'securitygroup': group_id,
        'subnet_id': subnet_id,
        'tags': 'Name=\"UGE compute node\"',
    }

    adapter_cfg.update(override_adapter_cfg)

    _update_resource_adapter_configuration(adapter_cfg, profile)

    print_statement('Resource adapter configuration completed successfully.')


def update_aws_credentials(region, access_key, secret_key):
    """
    Create ~/.aws/credentials
    """

    # Write "~/.aws/credentials"
    destpath = os.path.expandvars('$HOME/.aws')
    fn = os.path.join(destpath, 'credentials')

    print(
        colorama.Style.BRIGHT + colorama.Fore.GREEN +
        'Updating/creating AWS credentials (' +
        colorama.Fore.WHITE + fn + colorama.Fore.GREEN +
        ')... ' + colorama.Style.RESET_ALL, end='')

    if not os.path.exists(destpath):
        try:
            os.makedirs(destpath)
        except Exception as exc:
            print(
                colorama.Style.BRIGHT + colorama.Fore.RED + 'failed')

            errmsg = 'Unable to create directory [{0}]'.format(destpath)

            error_message(errmsg + ': {0}'.format(exc))

            sys.exit(1)

    try:
        contents = """\
[default]
region = {0}
aws_access_key_id = {1}
aws_secret_access_key = {2}
""".format(region, access_key, secret_key)

        # Backup destination
        if os.path.exists(fn):
            shutil.copyfile(fn, fn + '.orig')

        with open(fn, 'w') as fp:
            fp.write(contents)

        print(colorama.Style.BRIGHT + colorama.Fore.GREEN + 'done.')
    except Exception as exc:
        print(colorama.Style.BRIGHT + colorama.Fore.RED + 'failed.' +
                   colorama.Style.RESET_ALL)

        error_message('Unable to write/update [{0}]: {1}\n'.format(fn, exc))

        sys.exit(1)
    finally:
        print(colorama.Style.RESET_ALL, end='')


def _write_resource_adapter_configuration(adapter_cfg, profile):
    profile_ = 'resource-adapter' if profile == 'default' else profile
    cfg = configparser.ConfigParser()
    cfg.add_section(profile_)

    for key, value in adapter_cfg.items():
        cfg.set(profile_, key, value)

    fn = '/tmp/adapter-defaults-aws.conf'
    with open(fn, 'w') as fp:
        cfg.write(fp)

    print_statement('Wrote resource adapter configuration [{0}]', fn)


def error_message(msg, *args):
    print(
        format_string_with_arg(msg, *args, forecolour=colorama.Fore.RED))


def bright_white(msg):
    return colorama.Fore.WHITE + colorama.Style.BRIGHT + msg + \
        colorama.Style.RESET_ALL


def format_string_with_arg(msg, *args, **kwargs):
    forecolour = kwargs['forecolour'] \
        if 'forecolour' in kwargs else colorama.Fore.GREEN

    fmtarg = bright_white(args[0]) + colorama.Fore.GREEN if args else ''

    return forecolour + colorama.Style.BRIGHT + \
        msg.format(fmtarg) + colorama.Style.RESET_ALL


def print_statement(msg, *args):
    print(format_string_with_arg(msg, *args))


def _update_resource_adapter_configuration(adapter_cfg, profile_name):
    _write_resource_adapter_configuration(adapter_cfg, profile_name)

    normalized_cfg = []
    for key, value in adapter_cfg.items():
        normalized_cfg.append({
            'key': key,
            'value': value,
        })

    api = ResourceAdapterConfigurationApi()

    # check for resource adapter configuration
    with DbManager().session() as session:
        try:
            api.get(session, 'aws', profile_name)

            print_statement(
                'Updating AWS resource adapter configuration profile [{0}]',
                profile_name)

            api.update(session, 'aws', profile_name, normalized_cfg)
        except ResourceNotFound:
            print_statement(
                'Creating AWS resource adapter configuration profile [{0}]',
                profile_name)

            api.create(session, 'aws', profile_name, normalized_cfg)


def get_resource_name_from_tag(subnet):
    tags = [tag['Value'] for tag in subnet['Tags']
            if tag['Key'] == 'Name'] if 'Tags' in subnet else None

    name = tags[0] if tags else None

    return name
