# --------------------------------------------------------------------------------------------
# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License. See License.txt in the project root for license information.
# --------------------------------------------------------------------------------------------
# pylint: disable=line-too-long, consider-using-f-string, no-else-return, duplicate-string-formatting-argument, expression-not-assigned, too-many-locals, logging-fstring-interpolation, broad-except, pointless-statement, bare-except
from typing import Dict, Any
from urllib.parse import urlparse

from azure.cli.core.commands import AzCliCommand

import time

from azure.cli.core.azclierror import (
    RequiredArgumentMissingError,
    ValidationError,
    ArgumentUsageError,
    ResourceNotFoundError,
    MutuallyExclusiveArgumentError)
from azure.cli.core.commands.client_factory import get_subscription_id

from knack.log import get_logger
from knack.util import CLIError

from msrestazure.tools import parse_resource_id, is_valid_resource_id
from msrest.exceptions import DeserializationError

from .base_resource import BaseResource
from ._clients import ManagedEnvironmentClient, ConnectedEnvironmentClient, ManagedEnvironmentPreviewClient
from ._client_factory import handle_raw_exception, handle_non_404_status_code_exception

from ._models import (
    Ingress as IngressModel,
    Configuration as ConfigurationModel,
    Template as TemplateModel,
    RegistryCredentials as RegistryCredentialsModel,
    ContainerApp as ContainerAppModel,
    Dapr as DaprModel,
    ContainerResources as ContainerResourcesModel,
    Scale as ScaleModel,
    Service as ServiceModel,
    Container as ContainerModel,
    ManagedServiceIdentity as ManagedServiceIdentityModel,
    ScaleRule as ScaleRuleModel,
    Volume as VolumeModel,
    VolumeMount as VolumeMountModel)

from ._decorator_utils import (create_deserializer,
                               process_loaded_yaml,
                               load_yaml_file)
from ._utils import (_ensure_location_allowed,
                     parse_secret_flags, store_as_secret_and_return_secret_ref, parse_env_var_flags,
                     _convert_object_from_snake_to_camel_case,
                     _object_to_dict, _remove_additional_attributes,
                     _remove_readonly_attributes,
                     _infer_acr_credentials,
                     _ensure_identity_resource_id,
                     validate_container_app_name,
                     set_managed_identity,
                     create_acrpull_role_assignment, is_registry_msi_system,
                     safe_set, parse_metadata_flags, parse_auth_flags,
                     get_default_workload_profile_name_from_env,
                     ensure_workload_profile_supported, _generate_secret_volume_name,
                     parse_service_bindings, check_unique_bindings, AppType, get_linker_client,
                     safe_get, _update_revision_env_secretrefs, _add_or_update_tags, _populate_secret_values,
                     clean_null_values, _add_or_update_env_vars, _remove_env_vars, _get_existing_secrets, _get_acr_cred)
from ._validators import validate_create, validate_revision_suffix

from ._constants import (CONTAINER_APPS_RP,
                         HELLO_WORLD_IMAGE,
                         CONNECTED_ENVIRONMENT_TYPE,
                         CONNECTED_ENVIRONMENT_RESOURCE_TYPE,
                         MANAGED_ENVIRONMENT_TYPE,
                         MANAGED_ENVIRONMENT_RESOURCE_TYPE, ACR_IMAGE_SUFFIX)

from ._github_oauth import cache_github_token

logger = get_logger(__name__)


class BaseContainerAppDecorator(BaseResource):
    def __init__(self, cmd: AzCliCommand, client: Any, raw_parameters: Dict, models: str):
        super().__init__(cmd, client, raw_parameters, models)

    def list(self):
        containerapps = super().list()
        managed_env = self.get_argument_managed_env()
        if managed_env:
            env_name = parse_resource_id(managed_env)["name"].lower()
            if "resource_group" in parse_resource_id(managed_env):
                self.get_environment_client().show(self.cmd, parse_resource_id(managed_env)["resource_group"],
                                                   parse_resource_id(managed_env)["name"])
                containerapps = [c for c in containerapps if
                                 c["properties"]["environmentId"].lower() == managed_env.lower()]
            else:
                containerapps = [c for c in containerapps if
                                 parse_resource_id(c["properties"]["environmentId"])["name"].lower() == env_name]

        return containerapps

    def show(self):
        try:
            r = super().show()
            if self.get_param("show_secrets"):
                self.set_up_get_existing_secrets(r)
            return r
        except CLIError as e:
            handle_raw_exception(e)

    def list_secrets(self, show_values=False):
        containerapp_def = None
        try:
            containerapp_def = self.client.show(cmd=self.cmd, resource_group_name=self.get_argument_resource_group_name(), name=self.get_argument_name())
        except Exception as e:
            handle_non_404_status_code_exception(e)

        if not containerapp_def:
            raise ResourceNotFoundError("The containerapp '{}' does not exist".format(self.get_argument_name()))

        if not show_values:
            return safe_get(containerapp_def, "properties", "configuration", "secrets", default=[])

        try:
            return self.client.list_secrets(cmd=self.cmd, resource_group_name=self.get_argument_resource_group_name(), name=self.get_argument_name())["value"]
        except Exception as e:
            handle_non_404_status_code_exception(e)

    def get_environment_client(self):
        return ManagedEnvironmentClient

    def set_up_get_existing_secrets(self, containerapp_def):
        if "secrets" not in containerapp_def["properties"]["configuration"]:
            containerapp_def["properties"]["configuration"]["secrets"] = []
        else:
            secrets = None
            try:
                secrets = self.client.list_secrets(cmd=self.cmd, resource_group_name=self.get_argument_resource_group_name(), name=self.get_argument_name())
            except Exception as e:  # pylint: disable=broad-except
                handle_non_404_status_code_exception(e)

            containerapp_def["properties"]["configuration"]["secrets"] = secrets["value"]
            safe_set(containerapp_def, "properties", "configuration", "secrets", value=secrets["value"])

    def get_param(self, key) -> Any:
        return self.raw_param.get(key)

    def set_param(self, key, value):
        self.raw_param[key] = value

    def get_argument_name(self):
        return self.get_param("name")

    def get_argument_resource_group_name(self):
        return self.get_param("resource_group_name")

    def get_argument_no_wait(self):
        return self.get_param("no_wait")

    def get_argument_yaml(self):
        return self.get_param("yaml")

    def get_argument_image(self):
        return self.get_param("image")

    def set_argument_image(self, image):
        self.set_param("image", image)

    def get_argument_container_name(self):
        return self.get_param("container_name")

    def set_argument_container_name(self, container_name):
        self.set_param("container_name", container_name)

    def get_argument_managed_env(self):
        return self.get_param("managed_env")

    def set_argument_managed_env(self, managed_env):
        self.set_param("managed_env", managed_env)

    def get_argument_min_replicas(self):
        return self.get_param("min_replicas")

    def get_argument_max_replicas(self):
        return self.get_param("max_replicas")

    def get_argument_scale_rule_name(self):
        return self.get_param("scale_rule_name")

    def get_argument_scale_rule_type(self):
        return self.get_param("scale_rule_type")

    def set_argument_scale_rule_type(self, scale_rule_type):
        self.set_param("scale_rule_type", scale_rule_type)

    def get_argument_scale_rule_http_concurrency(self):
        return self.get_param("scale_rule_http_concurrency")

    def get_argument_scale_rule_metadata(self):
        return self.get_param("scale_rule_metadata")

    def get_argument_scale_rule_auth(self):
        return self.get_param("scale_rule_auth")

    def get_argument_target_port(self):
        return self.get_param("target_port")

    def get_argument_exposed_port(self):
        return self.get_param("exposed_port")

    def get_argument_transport(self):
        return self.get_param("transport")

    def get_argument_ingress(self):
        return self.get_param("ingress")

    def get_argument_allow_insecure(self):
        return self.get_param("allow_insecure")

    def get_argument_revisions_mode(self):
        return self.get_param("revisions_mode")

    def get_argument_secrets(self):
        return self.get_param("secrets")

    def get_argument_env_vars(self):
        return self.get_param("env_vars")

    def get_argument_cpu(self):
        return self.get_param("cpu")

    def get_argument_memory(self):
        return self.get_param("memory")

    def get_argument_registry_server(self):
        return self.get_param("registry_server")

    def get_argument_registry_user(self):
        return self.get_param("registry_user")

    def set_argument_registry_user(self, registry_user):
        self.set_param("registry_user", registry_user)

    def get_argument_registry_pass(self):
        return self.get_param("registry_pass")

    def set_argument_registry_pass(self, registry_pass):
        self.set_param("registry_pass", registry_pass)

    def get_argument_dapr_enabled(self):
        return self.get_param("dapr_enabled")

    def get_argument_dapr_app_port(self):
        return self.get_param("dapr_app_port")

    def get_argument_dapr_app_id(self):
        return self.get_param("dapr_app_id")

    def get_argument_dapr_app_protocol(self):
        return self.get_param("dapr_app_protocol")

    def get_argument_dapr_http_read_buffer_size(self):
        return self.get_param("dapr_http_read_buffer_size")

    def get_argument_dapr_http_max_request_size(self):
        return self.get_param("dapr_http_max_request_size")

    def get_argument_dapr_log_level(self):
        return self.get_param("dapr_log_level")

    def get_argument_dapr_enable_api_logging(self):
        return self.get_param("dapr_enable_api_logging")

    def get_argument_service_type(self):
        return self.get_param("service_type")

    def get_argument_service_bindings(self):
        return self.get_param("service_bindings")

    def get_argument_revision_suffix(self):
        return self.get_param("revision_suffix")

    def get_argument_startup_command(self):
        return self.get_param("startup_command")

    def get_argument_args(self):
        return self.get_param("args")

    def get_argument_tags(self):
        return self.get_param("tags")

    def get_argument_system_assigned(self):
        return self.get_param("system_assigned")

    def get_argument_disable_warnings(self):
        return self.get_param("disable_warnings")

    def get_argument_user_assigned(self):
        return self.get_param("user_assigned")

    def get_argument_registry_identity(self):
        return self.get_param("registry_identity")

    def get_argument_workload_profile_name(self):
        return self.get_param("workload_profile_name")

    def set_argument_workload_profile_name(self, workload_profile_name):
        self.set_param("workload_profile_name", workload_profile_name)

    def get_argument_secret_volume_mount(self):
        return self.get_param("secret_volume_mount")

    def get_argument_service_connectors_def_list(self):
        return self.get_param("service_connectors_def_list")

    def set_argument_service_connectors_def_list(self, service_connectors_def_list):
        self.set_param("service_connectors_def_list", service_connectors_def_list)

    def get_argument_termination_grace_period(self):
        return self.get_param("termination_grace_period")

    def get_argument_show_values(self):
        return self.get_param("show_values")

    def get_argument_set_env_vars(self):
        return self.get_param("set_env_vars")

    def get_argument_remove_env_vars(self):
        return self.get_param("remove_env_vars")

    def get_argument_replace_env_vars(self):
        return self.get_param("replace_env_vars")

    def get_argument_remove_all_env_vars(self):
        return self.get_param("remove_all_env_vars")

    def get_argument_from_revision(self):
        return self.get_param("from_revision")

    def get_argument_unbind_service_bindings(self):
        return self.get_param("unbind_service_bindings")


class ContainerAppCreateDecorator(BaseContainerAppDecorator):
    def __init__(
        self, cmd: AzCliCommand, client: Any, raw_parameters: Dict, models: str
    ):
        super().__init__(cmd, client, raw_parameters, models)
        self.containerapp_def = ContainerAppModel

    def validate_arguments(self):
        validate_container_app_name(self.get_argument_name(), AppType.ContainerApp.name)
        validate_create(self.get_argument_registry_identity(), self.get_argument_registry_pass(), self.get_argument_registry_user(), self.get_argument_registry_server(), self.get_argument_no_wait())
        validate_revision_suffix(self.get_argument_revision_suffix())

    def construct_payload(self):
        if self.get_argument_registry_identity() and not is_registry_msi_system(self.get_argument_registry_identity()):
            logger.info("Creating an acrpull role assignment for the registry identity")
            create_acrpull_role_assignment(self.cmd, self.get_argument_registry_server(), self.get_argument_registry_identity(), skip_error=True)

        if self.get_argument_yaml():
            return self.set_up_create_containerapp_yaml(name=self.get_argument_name(), file_name=self.get_argument_yaml())

        if not self.get_argument_image():
            self.set_argument_image(HELLO_WORLD_IMAGE)

        if self.get_argument_managed_env() is None:
            raise RequiredArgumentMissingError('Usage error: --environment is required if not using --yaml')

        # Validate managed environment
        parsed_managed_env = parse_resource_id(self.get_argument_managed_env())
        managed_env_name = parsed_managed_env['name']
        managed_env_rg = parsed_managed_env['resource_group']
        managed_env_info = None

        try:
            managed_env_info = self.get_environment_client().show(cmd=self.cmd, resource_group_name=managed_env_rg, name=managed_env_name)
        except Exception as e:
            handle_non_404_status_code_exception(e)

        if not managed_env_info:
            raise ValidationError("The environment '{}' does not exist. Specify a valid environment".format(self.get_argument_managed_env()))

        while not self.get_argument_no_wait() and safe_get(managed_env_info, "properties", "provisioningState", default="").lower() in ["inprogress", "updating"]:
            logger.info("Waiting for environment provisioning to finish before creating container app")
            time.sleep(5)
            managed_env_info = self.get_environment_client().show(cmd=self.cmd, resource_group_name=managed_env_rg, name=managed_env_name)

        location = managed_env_info["location"]
        _ensure_location_allowed(self.cmd, location, CONTAINER_APPS_RP, "containerApps")

        if not self.get_argument_workload_profile_name() and "workloadProfiles" in managed_env_info:
            workload_profile_name = get_default_workload_profile_name_from_env(self.cmd, managed_env_info, managed_env_rg)
            self.set_argument_workload_profile_name(workload_profile_name)

        external_ingress = None
        if self.get_argument_ingress() is not None:
            if self.get_argument_ingress().lower() == "internal":
                external_ingress = False
            elif self.get_argument_ingress().lower() == "external":
                external_ingress = True

        ingress_def = None
        if self.get_argument_target_port() is not None and self.get_argument_ingress() is not None:
            ingress_def = IngressModel
            ingress_def["external"] = external_ingress
            ingress_def["targetPort"] = self.get_argument_target_port()
            ingress_def["transport"] = self.get_argument_transport()
            ingress_def["exposedPort"] = self.get_argument_exposed_port() if self.get_argument_transport() == "tcp" else None
            ingress_def["allowInsecure"] = self.get_argument_allow_insecure()

        secrets_def = None
        if self.get_argument_secrets() is not None:
            secrets_def = parse_secret_flags(self.get_argument_secrets())

        registries_def = None
        if self.get_argument_registry_server() is not None and not is_registry_msi_system(self.get_argument_registry_identity()):
            registries_def = RegistryCredentialsModel
            registries_def["server"] = self.get_argument_registry_server()

            # Infer credentials if not supplied and its azurecr
            if (self.get_argument_registry_user() is None or self.get_argument_registry_pass() is None) and self.get_argument_registry_identity() is None:
                registry_user, registry_pass = _infer_acr_credentials(self.cmd, self.get_argument_registry_server(), self.get_argument_disable_warnings())
                self.set_argument_registry_user(registry_user)
                self.set_argument_registry_pass(registry_pass)

            if not self.get_argument_registry_identity():
                registries_def["username"] = self.get_argument_registry_user()

                if secrets_def is None:
                    secrets_def = []
                registries_def["passwordSecretRef"] = store_as_secret_and_return_secret_ref(secrets_def, self.get_argument_registry_user(),
                                                                                            self.get_argument_registry_server(),
                                                                                            self.get_argument_registry_pass(),
                                                                                            disable_warnings=self.get_argument_disable_warnings())
            else:
                registries_def["identity"] = self.get_argument_registry_identity()

        dapr_def = None
        if self.get_argument_dapr_enabled():
            dapr_def = DaprModel
            dapr_def["enabled"] = True
            dapr_def["appId"] = self.get_argument_dapr_app_id()
            dapr_def["appPort"] = self.get_argument_dapr_app_port()
            dapr_def["appProtocol"] = self.get_argument_dapr_app_protocol()
            dapr_def["httpReadBufferSize"] = self.get_argument_dapr_http_read_buffer_size()
            dapr_def["httpMaxRequestSize"] = self.get_argument_dapr_http_max_request_size()
            dapr_def["logLevel"] = self.get_argument_dapr_log_level()
            dapr_def["enableApiLogging"] = self.get_argument_dapr_enable_api_logging()

        service_def = None
        if self.get_argument_service_type():
            service_def = ServiceModel
            service_def["type"] = self.get_argument_service_type()

        config_def = ConfigurationModel
        config_def["secrets"] = secrets_def
        config_def["activeRevisionsMode"] = self.get_argument_revisions_mode()
        config_def["ingress"] = ingress_def
        config_def["registries"] = [registries_def] if registries_def is not None else None
        config_def["dapr"] = dapr_def
        config_def["service"] = service_def if service_def is not None else None

        # Identity actions
        identity_def = ManagedServiceIdentityModel
        identity_def["type"] = "None"

        assign_system_identity = self.get_argument_system_assigned()
        if self.get_argument_user_assigned():
            assign_user_identities = [x.lower() for x in self.get_argument_user_assigned()]
        else:
            assign_user_identities = []

        if assign_system_identity and assign_user_identities:
            identity_def["type"] = "SystemAssigned, UserAssigned"
        elif assign_system_identity:
            identity_def["type"] = "SystemAssigned"
        elif assign_user_identities:
            identity_def["type"] = "UserAssigned"

        if assign_user_identities:
            identity_def["userAssignedIdentities"] = {}
            subscription_id = get_subscription_id(self.cmd.cli_ctx)

            for r in assign_user_identities:
                r = _ensure_identity_resource_id(subscription_id, self.get_argument_resource_group_name(), r)
                identity_def["userAssignedIdentities"][r] = {}  # pylint: disable=unsupported-assignment-operation

        scale_def = self.set_up_scale_rule()

        resources_def = None
        if self.get_argument_cpu() is not None or self.get_argument_memory() is not None:
            resources_def = ContainerResourcesModel
            resources_def["cpu"] = self.get_argument_cpu()
            resources_def["memory"] = self.get_argument_memory()

        container_def = ContainerModel
        container_def["name"] = self.get_argument_container_name() if self.get_argument_container_name() else self.get_argument_name()
        container_def["image"] = self.get_argument_image() if not is_registry_msi_system(self.get_argument_registry_identity()) else HELLO_WORLD_IMAGE
        if self.get_argument_env_vars() is not None:
            container_def["env"] = parse_env_var_flags(self.get_argument_env_vars())
        if self.get_argument_startup_command() is not None:
            container_def["command"] = self.get_argument_startup_command()
        if self.get_argument_args() is not None:
            container_def["args"] = self.get_argument_args()
        if resources_def is not None:
            container_def["resources"] = resources_def

        template_def = TemplateModel

        template_def["containers"] = [container_def]
        template_def["scale"] = scale_def

        if self.get_argument_secret_volume_mount() is not None:
            volume_def = VolumeModel
            volume_mount_def = VolumeMountModel
            # generate a volume name
            volume_def["name"] = _generate_secret_volume_name()
            volume_def["storageType"] = "Secret"

            # mount the volume to the container
            volume_mount_def["volumeName"] = volume_def["name"]
            volume_mount_def["mountPath"] = self.get_argument_secret_volume_mount()
            container_def["volumeMounts"] = [volume_mount_def]
            template_def["volumes"] = [volume_def]

        if self.get_argument_revision_suffix() is not None and not is_registry_msi_system(self.get_argument_registry_identity()):
            template_def["revisionSuffix"] = self.get_argument_revision_suffix()

        if self.get_argument_termination_grace_period() is not None:
            template_def["terminationGracePeriodSeconds"] = self.get_argument_termination_grace_period()

        self.containerapp_def["location"] = location
        self.containerapp_def["identity"] = identity_def
        self.containerapp_def["properties"]["environmentId"] = self.get_argument_managed_env()
        self.containerapp_def["properties"]["configuration"] = config_def
        self.containerapp_def["properties"]["template"] = template_def
        self.containerapp_def["tags"] = self.get_argument_tags()

        if self.get_argument_workload_profile_name():
            self.containerapp_def["properties"]["workloadProfileName"] = self.get_argument_workload_profile_name()
            ensure_workload_profile_supported(self.cmd, managed_env_name, managed_env_rg, self.get_argument_workload_profile_name(),
                                              managed_env_info)

        if self.get_argument_registry_identity():
            if is_registry_msi_system(self.get_argument_registry_identity()):
                set_managed_identity(self.cmd, self.get_argument_resource_group_name(), self.containerapp_def, system_assigned=True)
            else:
                set_managed_identity(self.cmd, self.get_argument_resource_group_name(), self.containerapp_def, user_assigned=[self.get_argument_registry_identity()])

    def create(self):
        try:
            r = self.client.create_or_update(
                cmd=self.cmd, resource_group_name=self.get_argument_resource_group_name(), name=self.get_argument_name(), container_app_envelope=self.containerapp_def,
                no_wait=self.get_argument_no_wait())

            return r
        except Exception as e:
            handle_raw_exception(e)

    def construct_for_post_process(self, r):
        if is_registry_msi_system(self.get_argument_registry_identity()):
            while r["properties"]["provisioningState"] == "InProgress":
                r = self.client.show(self.cmd, self.get_argument_resource_group_name(), self.get_argument_name())
                time.sleep(10)
            logger.info("Creating an acrpull role assignment for the system identity")
            system_sp = r["identity"]["principalId"]
            create_acrpull_role_assignment(self.cmd, self.get_argument_registry_server(), registry_identity=None, service_principal=system_sp)
            containers_def = safe_get(self.containerapp_def, "properties", "template", "containers")
            containers_def[0]["image"] = self.get_argument_image()

            safe_set(self.containerapp_def, "properties", "template", "revisionSuffix", value=self.get_argument_revision_suffix())

            registries_def = RegistryCredentialsModel
            registries_def["server"] = self.get_argument_registry_server()
            registries_def["identity"] = self.get_argument_registry_identity()
            safe_set(self.containerapp_def, "properties", "configuration", "registries", value=[registries_def])

    def post_process(self, r):
        if is_registry_msi_system(self.get_argument_registry_identity()):
            r = self.create()

        if "properties" in r and "provisioningState" in r["properties"] and r["properties"]["provisioningState"].lower() == "waiting" and not self.get_argument_no_wait():
            not self.get_argument_disable_warnings() and logger.warning('Containerapp creation in progress. Please monitor the creation using `az containerapp show -n {} -g {}`'.format(self.get_argument_name(), self.get_argument_resource_group_name()))

        if "configuration" in r["properties"] and "ingress" in r["properties"]["configuration"] and r["properties"]["configuration"]["ingress"] and "fqdn" in r["properties"]["configuration"]["ingress"]:
            not self.get_argument_disable_warnings() and logger.warning("\nContainer app created. Access your app at https://{}/\n".format(r["properties"]["configuration"]["ingress"]["fqdn"]))
        else:
            target_port = self.get_argument_target_port() or "<port>"
            not self.get_argument_disable_warnings() and logger.warning("\nContainer app created. To access it over HTTPS, enable ingress: "
                                                                        "az containerapp ingress enable -n %s -g %s --type external --target-port %s"
                                                                        " --transport auto\n", self.get_argument_name(), self.get_argument_resource_group_name(), target_port)

        return r

    def set_up_create_containerapp_yaml(self, name, file_name):
        if self.get_argument_image() or self.get_argument_min_replicas() or self.get_argument_max_replicas() or self.get_argument_target_port() or self.get_argument_ingress() or \
                self.get_argument_revisions_mode() or self.get_argument_secrets() or self.get_argument_env_vars() or self.get_argument_cpu() or self.get_argument_memory() or self.get_argument_registry_server() or \
                self.get_argument_registry_user() or self.get_argument_registry_pass() or self.get_argument_dapr_enabled() or self.get_argument_dapr_app_port() or self.get_argument_dapr_app_id() or \
                self.get_argument_startup_command() or self.get_argument_args() or self.get_argument_tags():
            not self.get_argument_disable_warnings() and logger.warning(
                'Additional flags were passed along with --yaml. These flags will be ignored, and the configuration defined in the yaml will be used instead')

        yaml_containerapp = process_loaded_yaml(load_yaml_file(file_name))
        if type(yaml_containerapp) != dict:  # pylint: disable=unidiomatic-typecheck
            raise ValidationError(
                'Invalid YAML provided. Please see https://aka.ms/azure-container-apps-yaml for a valid containerapps YAML spec.')

        if not yaml_containerapp.get('name'):
            yaml_containerapp['name'] = name
        elif yaml_containerapp.get('name').lower() != name.lower():
            logger.warning(
                'The app name provided in the --yaml file "{}" does not match the one provided in the --name flag "{}". The one provided in the --yaml file will be used.'.format(
                    yaml_containerapp.get('name'), name))
        name = yaml_containerapp.get('name')

        if not yaml_containerapp.get('type'):
            yaml_containerapp['type'] = 'Microsoft.App/containerApps'
        elif yaml_containerapp.get('type').lower() != "microsoft.app/containerapps":
            raise ValidationError('Containerapp type must be \"Microsoft.App/ContainerApps\"')

        # Deserialize the yaml into a ContainerApp object. Need this since we're not using SDK
        try:
            deserializer = create_deserializer(self.models)

            self.containerapp_def = deserializer('ContainerApp', yaml_containerapp)
        except DeserializationError as ex:
            raise ValidationError(
                'Invalid YAML provided. Please see https://aka.ms/azure-container-apps-yaml for a valid containerapps YAML spec.') from ex

        # Remove tags before converting from snake case to camel case, then re-add tags. We don't want to change the case of the tags. Need this since we're not using SDK
        tags = None
        if yaml_containerapp.get('tags'):
            tags = yaml_containerapp.get('tags')
            del yaml_containerapp['tags']

        self.containerapp_def = _convert_object_from_snake_to_camel_case(_object_to_dict(self.containerapp_def))
        self.containerapp_def['tags'] = tags

        # After deserializing, some properties may need to be moved under the "properties" attribute. Need this since we're not using SDK
        self.containerapp_def = process_loaded_yaml(self.containerapp_def)

        # Remove "additionalProperties" and read-only attributes that are introduced in the deserialization. Need this since we're not using SDK
        _remove_additional_attributes(self.containerapp_def)
        _remove_readonly_attributes(self.containerapp_def)

        # Remove extra workloadProfileName introduced in deserialization
        if "workloadProfileName" in self.containerapp_def:
            del self.containerapp_def["workloadProfileName"]

        # Validate managed environment
        env_id = self.containerapp_def["properties"]['environmentId']
        env_info = None
        if self.get_argument_managed_env():
            if not self.get_argument_disable_warnings() and env_id is not None and env_id != self.get_argument_managed_env():
                logger.warning('The environmentId was passed along with --yaml. The value entered with --environment will be ignored, and the configuration defined in the yaml will be used instead')
            if env_id is None:
                env_id = self.get_argument_managed_env()
                safe_set(self.containerapp_def, "properties", "environmentId", value=env_id)

        if not self.containerapp_def["properties"].get('environmentId'):
            raise RequiredArgumentMissingError(
                'environmentId is required. This can be retrieved using the `az containerapp env show -g MyResourceGroup -n MyContainerappEnvironment --query id` command. Please see https://aka.ms/azure-container-apps-yaml for a valid containerapps YAML spec.')

        if is_valid_resource_id(env_id):
            parsed_managed_env = parse_resource_id(env_id)
            env_name = parsed_managed_env['name']
            env_rg = parsed_managed_env['resource_group']
        else:
            raise ValidationError('Invalid environmentId specified. Environment not found')

        try:
            env_info = self.get_environment_client().show(cmd=self.cmd, resource_group_name=env_rg, name=env_name)
        except Exception as e:
            handle_non_404_status_code_exception(e)

        if not env_info:
            raise ValidationError("The environment '{}' in resource group '{}' was not found".format(env_name, env_rg))

        # Validate location
        if not self.containerapp_def.get('location'):
            self.containerapp_def['location'] = env_info['location']

    def set_up_scale_rule(self):
        scale_def = None
        if self.get_argument_min_replicas() is not None or self.get_argument_max_replicas() is not None:
            scale_def = ScaleModel
            scale_def["minReplicas"] = self.get_argument_min_replicas()
            scale_def["maxReplicas"] = self.get_argument_max_replicas()

        scale_rule_type = self.get_argument_scale_rule_type()
        scale_rule_name = self.get_argument_scale_rule_name()
        scale_rule_auth = self.get_argument_scale_rule_auth()
        scale_rule_metadata = self.get_argument_scale_rule_metadata()
        scale_rule_http_concurrency = self.get_argument_scale_rule_http_concurrency()
        if self.get_argument_scale_rule_name():
            if not scale_rule_type:
                scale_rule_type = "http"
            scale_rule_type = scale_rule_type.lower()
            scale_rule_def = ScaleRuleModel
            curr_metadata = {}
            if self.get_argument_scale_rule_http_concurrency():
                if scale_rule_type in ('http', 'tcp'):
                    curr_metadata["concurrentRequests"] = str(scale_rule_http_concurrency)
            metadata_def = parse_metadata_flags(scale_rule_metadata, curr_metadata)
            auth_def = parse_auth_flags(scale_rule_auth)
            if scale_rule_type == "http":
                scale_rule_def["name"] = scale_rule_name
                scale_rule_def["custom"] = None
                scale_rule_def["http"] = {}
                scale_rule_def["http"]["metadata"] = metadata_def
                scale_rule_def["http"]["auth"] = auth_def
            else:
                scale_rule_def["name"] = scale_rule_name
                scale_rule_def["http"] = None
                scale_rule_def["custom"] = {}
                scale_rule_def["custom"]["type"] = scale_rule_type
                scale_rule_def["custom"]["metadata"] = metadata_def
                scale_rule_def["custom"]["auth"] = auth_def
            if not scale_def:
                scale_def = ScaleModel
            scale_def["rules"] = [scale_rule_def]

        return scale_def


class ContainerAppUpdateDecorator(BaseContainerAppDecorator):
    def __init__(
        self, cmd: AzCliCommand, client: Any, raw_parameters: Dict, models: str
    ):
        super().__init__(cmd, client, raw_parameters, models)
        self.containerapp_def = {}
        self.new_containerapp = {}

    def validate_arguments(self):
        validate_revision_suffix(self.get_argument_revision_suffix())
        # Validate that max_replicas is set to 0-1000
        if self.get_argument_max_replicas() is not None:
            if self.get_argument_max_replicas() < 1 or self.get_argument_max_replicas() > 1000:
                raise ArgumentUsageError('--max-replicas must be in the range [1,1000]')

    def update(self):
        try:
            r = self.client.update(
                cmd=self.cmd, resource_group_name=self.get_argument_resource_group_name(), name=self.get_argument_name(), container_app_envelope=self.new_containerapp,
                no_wait=self.get_argument_no_wait())
            if not self.get_argument_no_wait() and "properties" in r and "provisioningState" in r["properties"] and r["properties"]["provisioningState"].lower() == "waiting":
                logger.warning('Containerapp update in progress. Please monitor the update using `az containerapp show -n {} -g {}`'.format(self.get_argument_name(), self.get_argument_resource_group_name()))
            return r
        except Exception as e:
            handle_raw_exception(e)

    def set_up_from_revision(self):
        if self.get_argument_from_revision():
            r = None
            try:
                r = self.client.show_revision(cmd=self.cmd, resource_group_name=self.get_argument_resource_group_name(), container_app_name=self.get_argument_name(), name=self.get_argument_from_revision())
            except CLIError as e:
                handle_non_404_status_code_exception(e)

            _update_revision_env_secretrefs(r["properties"]["template"]["containers"], self.get_argument_name())
            safe_set(self.new_containerapp, "properties", "template", value=r["properties"]["template"])

    def _need_update_container(self):
        return self.get_argument_image() or self.get_argument_container_name() or self.get_argument_set_env_vars() is not None or self.get_argument_remove_env_vars() is not None or self.get_argument_replace_env_vars() is not None or self.get_argument_remove_all_env_vars() or self.get_argument_cpu() or self.get_argument_memory() or self.get_argument_startup_command() is not None or self.get_argument_args() is not None or self.get_argument_secret_volume_mount() is not None

    def construct_payload(self):
        # construct from yaml
        if self.get_argument_yaml():
            return self.set_up_update_containerapp_yaml(name=self.get_argument_name(), file_name=self.get_argument_yaml())

        self.containerapp_def = None
        try:
            self.containerapp_def = self.client.show(cmd=self.cmd, resource_group_name=self.get_argument_resource_group_name(), name=self.get_argument_name())
        except Exception as e:
            handle_non_404_status_code_exception(e)

        if not self.containerapp_def:
            raise ResourceNotFoundError("The containerapp '{}' does not exist".format(self.get_argument_name()))

        self.new_containerapp["properties"] = {}

        self.set_up_from_revision()

        # Doing this while API has bug. If env var is an empty string, API doesn't return "value" even though the "value" should be an empty string
        for container in safe_get(self.containerapp_def, "properties", "template", "containers", default=[]):
            if "env" in container:
                for e in container["env"]:
                    if "value" not in e:
                        e["value"] = ""

        update_map = {}
        update_map['scale'] = self.get_argument_min_replicas() or self.get_argument_max_replicas() or self.get_argument_scale_rule_name()
        update_map['container'] = self._need_update_container()
        update_map['ingress'] = self.get_argument_ingress() or self.get_argument_target_port()
        update_map['registry'] = self.get_argument_registry_server() or self.get_argument_registry_user() or self.get_argument_registry_pass()

        if self.get_argument_tags():
            _add_or_update_tags(self.new_containerapp, self.get_argument_tags())

        if self.get_argument_revision_suffix() is not None:
            self.new_containerapp["properties"]["template"] = {} if "template" not in self.new_containerapp["properties"] else self.new_containerapp["properties"]["template"]
            self.new_containerapp["properties"]["template"]["revisionSuffix"] = self.get_argument_revision_suffix()

        if self.get_argument_termination_grace_period() is not None:
            safe_set(self.new_containerapp, "properties", "template", "terminationGracePeriodSeconds",
                     value=self.get_argument_termination_grace_period())

        if self.get_argument_workload_profile_name():
            self.new_containerapp["properties"]["workloadProfileName"] = self.get_argument_workload_profile_name()

            parsed_managed_env = parse_resource_id(self.containerapp_def["properties"]["environmentId"])
            managed_env_name = parsed_managed_env['name']
            managed_env_rg = parsed_managed_env['resource_group']
            managed_env_info = None
            try:
                managed_env_info = self.get_environment_client().show(cmd=self.cmd, resource_group_name=managed_env_rg, name=managed_env_name)
            except Exception as e:
                handle_non_404_status_code_exception(e)

            if not managed_env_info:
                raise ValidationError(
                    "Error parsing the managed environment '{}' from the specified containerapp".format(
                        managed_env_name))

            ensure_workload_profile_supported(self.cmd, managed_env_name, managed_env_rg, self.get_argument_workload_profile_name(),
                                              managed_env_info)

        # Containers
        if update_map["container"]:
            self.new_containerapp["properties"]["template"] = {} if "template" not in self.new_containerapp["properties"] else self.new_containerapp["properties"]["template"]
            self.new_containerapp["properties"]["template"]["containers"] = self.containerapp_def["properties"]["template"]["containers"]
            if not self.get_argument_container_name():
                if len(self.new_containerapp["properties"]["template"]["containers"]) == 1:
                    container_name = self.new_containerapp["properties"]["template"]["containers"][0]["name"]
                    self.set_argument_container_name(container_name)
                else:
                    raise ValidationError(
                        "Usage error: --container-name is required when adding or updating a container")

            # Check if updating existing container
            updating_existing_container = False
            for c in self.new_containerapp["properties"]["template"]["containers"]:
                if c["name"].lower() == self.get_argument_container_name().lower():
                    updating_existing_container = True

                    if self.get_argument_image() is not None:
                        c["image"] = self.get_argument_image()

                    if self.get_argument_set_env_vars() is not None:
                        if "env" not in c or not c["env"]:
                            c["env"] = []
                        # env vars
                        _add_or_update_env_vars(c["env"], parse_env_var_flags(self.get_argument_set_env_vars()))

                    if self.get_argument_replace_env_vars() is not None:
                        # Remove other existing env_vars, then add them
                        c["env"] = []
                        _add_or_update_env_vars(c["env"], parse_env_var_flags(self.get_argument_replace_env_vars()))

                    if self.get_argument_remove_env_vars() is not None:
                        if "env" not in c or not c["env"]:
                            c["env"] = []
                        # env vars
                        _remove_env_vars(c["env"], self.get_argument_remove_env_vars())

                    if self.get_argument_remove_all_env_vars():
                        c["env"] = []

                    if self.get_argument_startup_command() is not None:
                        if isinstance(self.get_argument_startup_command(), list) and not self.get_argument_startup_command():
                            c["command"] = None
                        else:
                            c["command"] = self.get_argument_startup_command()
                    if self.get_argument_args() is not None:
                        if isinstance(self.get_argument_args(), list) and not self.get_argument_args():
                            c["args"] = None
                        else:
                            c["args"] = self.get_argument_args()
                    if self.get_argument_cpu() is not None or self.get_argument_memory() is not None:
                        if "resources" in c and c["resources"]:
                            if self.get_argument_cpu() is not None:
                                c["resources"]["cpu"] = self.get_argument_cpu()
                            if self.get_argument_memory() is not None:
                                c["resources"]["memory"] = self.get_argument_memory()
                        else:
                            c["resources"] = {
                                "cpu": self.get_argument_cpu(),
                                "memory": self.get_argument_memory()
                            }
                    if self.get_argument_secret_volume_mount() is not None:
                        self.new_containerapp["properties"]["template"]["volumes"] = self.containerapp_def["properties"]["template"]["volumes"]
                        if "volumeMounts" not in c or not c["volumeMounts"]:
                            # if no volume mount exists, create a new volume and then mount
                            volume_def = VolumeModel
                            volume_mount_def = VolumeMountModel
                            volume_def["name"] = _generate_secret_volume_name()
                            volume_def["storageType"] = "Secret"

                            volume_mount_def["volumeName"] = volume_def["name"]
                            volume_mount_def["mountPath"] = self.get_argument_secret_volume_mount()

                            if "volumes" not in self.new_containerapp["properties"]["template"]:
                                self.new_containerapp["properties"]["template"]["volumes"] = [volume_def]
                            else:
                                self.new_containerapp["properties"]["template"]["volumes"].append(volume_def)
                            c["volumeMounts"] = volume_mount_def
                        else:
                            if len(c["volumeMounts"]) > 1:
                                raise ValidationError(
                                    "Usage error: --secret-volume-mount can only be used with a container that has a single volume mount, to define multiple volumes and mounts please use --yaml")
                            else:
                                # check that the only volume is of type secret
                                volume_name = c["volumeMounts"][0]["volumeName"]
                                for v in self.new_containerapp["properties"]["template"]["volumes"]:
                                    if v["name"].lower() == volume_name.lower():
                                        if v["storageType"] != "Secret":
                                            raise ValidationError(
                                                "Usage error: --secret-volume-mount can only be used to update volume mounts with volumes of type secret. To update other types of volumes please use --yaml")
                                        break
                                c["volumeMounts"][0]["mountPath"] = self.get_argument_secret_volume_mount()

            # If not updating existing container, add as new container
            if not updating_existing_container:
                if self.get_argument_image() is None:
                    raise ValidationError("Usage error: --image is required when adding a new container")

                resources_def = None
                if self.get_argument_cpu() is not None or self.get_argument_memory() is not None:
                    resources_def = ContainerResourcesModel
                    resources_def["cpu"] = self.get_argument_cpu()
                    resources_def["memory"] = self.get_argument_memory()

                container_def = ContainerModel
                container_def["name"] = self.get_argument_container_name()
                container_def["image"] = self.get_argument_image()
                container_def["env"] = []

                if self.get_argument_set_env_vars() is not None:
                    # env vars
                    _add_or_update_env_vars(container_def["env"], parse_env_var_flags(self.get_argument_set_env_vars()))

                if self.get_argument_replace_env_vars() is not None:
                    # env vars
                    _add_or_update_env_vars(container_def["env"], parse_env_var_flags(self.get_argument_replace_env_vars()))

                if self.get_argument_remove_env_vars() is not None:
                    # env vars
                    _remove_env_vars(container_def["env"], self.get_argument_remove_env_vars())

                if self.get_argument_remove_all_env_vars():
                    container_def["env"] = []

                if self.get_argument_startup_command() is not None:
                    if isinstance(self.get_argument_startup_command(), list) and not self.get_argument_startup_command():
                        container_def["command"] = None
                    else:
                        container_def["command"] = self.get_argument_startup_command()
                if self.get_argument_args() is not None:
                    if isinstance(self.get_argument_args(), list) and not self.get_argument_args():
                        container_def["args"] = None
                    else:
                        container_def["args"] = self.get_argument_args()
                if resources_def is not None:
                    container_def["resources"] = resources_def
                if self.get_argument_secret_volume_mount() is not None:
                    self.new_containerapp["properties"]["template"]["volumes"] = self.containerapp_def["properties"]["template"]["volumes"]
                    # generate a new volume name
                    volume_def = VolumeModel
                    volume_mount_def = VolumeMountModel
                    volume_def["name"] = _generate_secret_volume_name()
                    volume_def["storageType"] = "Secret"

                    # mount the volume to the container
                    volume_mount_def["volumeName"] = volume_def["name"]
                    volume_mount_def["mountPath"] = self.get_argument_secret_volume_mount()
                    container_def["volumeMounts"] = [volume_mount_def]
                    if "volumes" not in self.new_containerapp["properties"]["template"]:
                        self.new_containerapp["properties"]["template"]["volumes"] = [volume_def]
                    else:
                        self.new_containerapp["properties"]["template"]["volumes"].append(volume_def)

                self.new_containerapp["properties"]["template"]["containers"].append(container_def)
        # Scale
        if update_map["scale"]:
            self.new_containerapp["properties"]["template"] = {} if "template" not in self.new_containerapp["properties"] else self.new_containerapp["properties"]["template"]
            if "scale" not in self.new_containerapp["properties"]["template"]:
                self.new_containerapp["properties"]["template"]["scale"] = {}
            if self.get_argument_min_replicas() is not None:
                self.new_containerapp["properties"]["template"]["scale"]["minReplicas"] = self.get_argument_min_replicas()
            if self.get_argument_max_replicas() is not None:
                self.new_containerapp["properties"]["template"]["scale"]["maxReplicas"] = self.get_argument_max_replicas()

        scale_def = None
        if self.get_argument_min_replicas() is not None or self.get_argument_max_replicas() is not None:
            scale_def = ScaleModel
            scale_def["minReplicas"] = self.get_argument_min_replicas()
            scale_def["maxReplicas"] = self.get_argument_max_replicas()
        # so we don't overwrite rules
        if safe_get(self.new_containerapp, "properties", "template", "scale", "rules"):
            self.new_containerapp["properties"]["template"]["scale"].pop(["rules"])
        scale_rule_type = self.get_argument_scale_rule_type()
        if self.get_argument_scale_rule_name():
            if not scale_rule_type:
                scale_rule_type = "http"
            scale_rule_type = scale_rule_type.lower()
            scale_rule_def = ScaleRuleModel
            curr_metadata = {}
            if self.get_argument_scale_rule_http_concurrency():
                if scale_rule_type in ('http', 'tcp'):
                    curr_metadata["concurrentRequests"] = str(self.get_argument_scale_rule_http_concurrency())
            metadata_def = parse_metadata_flags(self.get_argument_scale_rule_metadata(), curr_metadata)
            auth_def = parse_auth_flags(self.get_argument_scale_rule_auth())
            if scale_rule_type == "http":
                scale_rule_def["name"] = self.get_argument_scale_rule_name()
                scale_rule_def["custom"] = None
                scale_rule_def["http"] = {}
                scale_rule_def["http"]["metadata"] = metadata_def
                scale_rule_def["http"]["auth"] = auth_def
            else:
                scale_rule_def["name"] = self.get_argument_scale_rule_name()
                scale_rule_def["http"] = None
                scale_rule_def["custom"] = {}
                scale_rule_def["custom"]["type"] = scale_rule_type
                scale_rule_def["custom"]["metadata"] = metadata_def
                scale_rule_def["custom"]["auth"] = auth_def
            if not scale_def:
                scale_def = ScaleModel
            scale_def["rules"] = [scale_rule_def]
            self.new_containerapp["properties"]["template"]["scale"]["rules"] = scale_def["rules"]
        # Ingress
        if update_map["ingress"]:
            self.new_containerapp["properties"]["configuration"] = {} if "configuration" not in self.new_containerapp[
                "properties"] else self.new_containerapp["properties"]["configuration"]
            if self.get_argument_target_port() is not None or self.get_argument_ingress() is not None:
                self.new_containerapp["properties"]["configuration"]["ingress"] = {}
                if self.get_argument_ingress():
                    self.new_containerapp["properties"]["configuration"]["ingress"][
                        "external"] = self.get_argument_ingress().lower() == "external"
                if self.get_argument_target_port():
                    self.new_containerapp["properties"]["configuration"]["ingress"]["targetPort"] = self.get_argument_target_port()

        # Registry
        if update_map["registry"]:
            self.new_containerapp["properties"]["configuration"] = {} if "configuration" not in self.new_containerapp[
                "properties"] else self.new_containerapp["properties"]["configuration"]
            if "registries" in self.containerapp_def["properties"]["configuration"]:
                self.new_containerapp["properties"]["configuration"]["registries"] = self.containerapp_def["properties"]["configuration"]["registries"]
            if "registries" not in self.containerapp_def["properties"]["configuration"] or \
                    self.containerapp_def["properties"]["configuration"]["registries"] is None:
                self.new_containerapp["properties"]["configuration"]["registries"] = []

            registries_def = self.new_containerapp["properties"]["configuration"]["registries"]

            self.set_up_get_existing_secrets(self.containerapp_def)
            if "secrets" in self.containerapp_def["properties"]["configuration"] and self.containerapp_def["properties"]["configuration"]["secrets"]:
                self.new_containerapp["properties"]["configuration"]["secrets"] = self.containerapp_def["properties"]["configuration"]["secrets"]
            else:
                self.new_containerapp["properties"]["configuration"]["secrets"] = []

            if self.get_argument_registry_server():
                if not self.get_argument_registry_pass() or not self.get_argument_registry_user():
                    if ACR_IMAGE_SUFFIX not in self.get_argument_registry_server():
                        raise RequiredArgumentMissingError(
                            'Registry url is required if using Azure Container Registry, otherwise Registry username and password are required if using Dockerhub')
                    logger.warning(
                        'No credential was provided to access Azure Container Registry. Trying to look up...')
                    parsed = urlparse(self.get_argument_registry_server())
                    registry_name = (parsed.netloc if parsed.scheme else parsed.path).split('.')[0]
                    registry_user, registry_pass, _ = _get_acr_cred(self.cmd.cli_ctx, registry_name)
                    self.set_argument_registry_user(registry_user)
                    self.set_argument_registry_pass(registry_pass)

                # Check if updating existing registry
                updating_existing_registry = False
                for r in registries_def:
                    if r['server'].lower() == self.get_argument_registry_server().lower():
                        updating_existing_registry = True
                        if self.get_argument_registry_user():
                            r["username"] = self.get_argument_registry_user()
                        if self.get_argument_registry_pass():
                            r["passwordSecretRef"] = store_as_secret_and_return_secret_ref(
                                self.new_containerapp["properties"]["configuration"]["secrets"],
                                r["username"],
                                r["server"],
                                self.get_argument_registry_pass(),
                                update_existing_secret=True,
                                disable_warnings=True)

                # If not updating existing registry, add as new registry
                if not updating_existing_registry:
                    registry = RegistryCredentialsModel
                    registry["server"] = self.get_argument_registry_server()
                    registry["username"] = self.get_argument_registry_user()
                    registry["passwordSecretRef"] = store_as_secret_and_return_secret_ref(
                        self.new_containerapp["properties"]["configuration"]["secrets"],
                        self.get_argument_registry_user(),
                        self.get_argument_registry_server(),
                        self.get_argument_registry_pass(),
                        update_existing_secret=True,
                        disable_warnings=True)

                    registries_def.append(registry)

        if not self.get_argument_revision_suffix():
            self.new_containerapp["properties"]["template"] = {} if "template" not in self.new_containerapp["properties"] else self.new_containerapp["properties"]["template"]
            self.new_containerapp["properties"]["template"]["revisionSuffix"] = None

    def set_up_update_containerapp_yaml(self, name, file_name):
        if self.get_argument_image() or self.get_argument_min_replicas() or self.get_argument_max_replicas() or \
                self.get_argument_set_env_vars() or self.get_argument_remove_env_vars() or self.get_argument_replace_env_vars() or self.get_argument_remove_all_env_vars() or self.get_argument_cpu() or self.get_argument_memory() or \
                self.get_argument_startup_command() or self.get_argument_args() or self.get_argument_tags():
            logger.warning(
                'Additional flags were passed along with --yaml. These flags will be ignored, and the configuration defined in the yaml will be used instead')
        yaml_containerapp = process_loaded_yaml(load_yaml_file(file_name))
        if type(yaml_containerapp) != dict:  # pylint: disable=unidiomatic-typecheck
            raise ValidationError(
                'Invalid YAML provided. Please see https://aka.ms/azure-container-apps-yaml for a valid containerapps YAML spec.')

        if not yaml_containerapp.get('name'):
            yaml_containerapp['name'] = name
        elif yaml_containerapp.get('name').lower() != name.lower():
            logger.warning(
                'The app name provided in the --yaml file "{}" does not match the one provided in the --name flag "{}". The one provided in the --yaml file will be used.'.format(
                    yaml_containerapp.get('name'), name))
        name = yaml_containerapp.get('name')

        if not yaml_containerapp.get('type'):
            yaml_containerapp['type'] = 'Microsoft.App/containerApps'
        elif yaml_containerapp.get('type').lower() != "microsoft.app/containerapps":
            raise ValidationError('Containerapp type must be \"Microsoft.App/ContainerApps\"')

        # Check if containerapp exists
        try:
            self.new_containerapp = self.client.show(cmd=self.cmd, resource_group_name=self.get_argument_resource_group_name(), name=self.get_argument_name())
        except Exception as e:
            handle_non_404_status_code_exception(e)

        if not self.new_containerapp:
            raise ValidationError("The containerapp '{}' does not exist".format(name))
        existed_environment_id = self.new_containerapp['properties']['environmentId']
        self.new_containerapp = None

        # Deserialize the yaml into a ContainerApp object. Need this since we're not using SDK
        try:
            deserializer = create_deserializer(self.models)
            self.new_containerapp = deserializer('ContainerApp', yaml_containerapp)
        except DeserializationError as ex:
            raise ValidationError(
                'Invalid YAML provided. Please see https://aka.ms/azure-container-apps-yaml for a valid containerapps YAML spec.') from ex

        # Remove tags before converting from snake case to camel case, then re-add tags. We don't want to change the case of the tags. Need this since we're not using SDK
        tags = None
        if yaml_containerapp.get('tags'):
            tags = yaml_containerapp.get('tags')
            del yaml_containerapp['tags']

        self.new_containerapp = _convert_object_from_snake_to_camel_case(_object_to_dict(self.new_containerapp))
        self.new_containerapp['tags'] = tags

        # After deserializing, some properties may need to be moved under the "properties" attribute. Need this since we're not using SDK
        self.new_containerapp = process_loaded_yaml(self.new_containerapp)

        # Change which revision we update from
        if self.get_argument_from_revision():
            r = self.client.show_revision(cmd=self.cmd, resource_group_name=self.get_argument_resource_group_name(), container_app_name=name, name=self.get_argument_from_revision())
            _update_revision_env_secretrefs(r["properties"]["template"]["containers"], name)
            self.new_containerapp["properties"]["template"] = r["properties"]["template"]

        # Remove "additionalProperties" and read-only attributes that are introduced in the deserialization. Need this since we're not using SDK
        _remove_additional_attributes(self.new_containerapp)
        _remove_readonly_attributes(self.new_containerapp)

        secret_values = self.list_secrets(show_values=True)
        _populate_secret_values(self.new_containerapp, secret_values)

        # Clean null values since this is an update
        self.new_containerapp = clean_null_values(self.new_containerapp)

        # Fix bug with revisionSuffix when containers are added
        if not safe_get(self.new_containerapp, "properties", "template", "revisionSuffix"):
            if "properties" not in self.new_containerapp:
                self.new_containerapp["properties"] = {}
            if "template" not in self.new_containerapp["properties"]:
                self.new_containerapp["properties"]["template"] = {}
            self.new_containerapp["properties"]["template"]["revisionSuffix"] = None

        # Remove the environmentId in the PATCH payload if it has not been changed
        if safe_get(self.new_containerapp, "properties", "environmentId") and safe_get(self.new_containerapp, "properties", "environmentId").lower() == existed_environment_id.lower():
            del self.new_containerapp["properties"]['environmentId']


# decorator for preview create
class ContainerAppPreviewCreateDecorator(ContainerAppCreateDecorator):
    def __init__(
        self, cmd: AzCliCommand, client: Any, raw_parameters: Dict, models: str
    ):
        super().__init__(cmd, client, raw_parameters, models)

    def construct_payload(self):
        super().construct_payload()
        self.set_up_service_binds()
        self.set_up_extended_location()
        self.set_up_source()
        self.set_up_repo()

    def validate_arguments(self):
        super().validate_arguments()
        validate_create(self.get_argument_registry_identity(), self.get_argument_registry_pass(), self.get_argument_registry_user(), self.get_argument_registry_server(), self.get_argument_no_wait(), self.get_argument_source(), self.get_argument_repo(), self.get_argument_yaml(), self.get_argument_environment_type())

    def set_up_source(self):
        if self.get_argument_source():
            from ._up_utils import _has_dockerfile
            app, env = self._construct_app_and_env_for_source_or_repo()
            dockerfile = "Dockerfile"
            has_dockerfile = _has_dockerfile(self.get_argument_source(), dockerfile)
            # Uses buildpacks or an ACR Task to generate image if Dockerfile was not provided by the user
            app.run_acr_build(dockerfile, self.get_argument_source(), quiet=False, build_from_source=not has_dockerfile)
            # Validate containers exist
            containers = safe_get(self.containerapp_def, "properties", "template", "containers", default=[])
            if containers is None or len(containers) == 0:
                raise ValidationError(
                    "The container app '{}' does not have any containers. Please use --image to set the image for the container app".format(
                        self.get_argument_name()))
            # Update image
            containers[0]["image"] = HELLO_WORLD_IMAGE if app.image is None else app.image

    def set_up_repo(self):
        if self.get_argument_repo():
            # Check if container app exists and if it does, set the image to the image of the existing container app
            existing_container_app = self._get_containerapp_if_exists()
            if existing_container_app:
                # Executing the create command with --repo makes two ARM calls. The container app is created first
                # and the source control is set only after the container app is created. This means, when the container app is
                # first created, the image would be defaulted to mcr.microsoft.com/k8se/quickstart:latest (if --image was not provided)
                # and the image name would change to `registry-server/containerapp-name:{github-sha}` only
                # after the source control completes successfully
                # and the new image is deployed.
                # If the create command is executed multiple times however, we need to make sure that the image is not defaulted
                # to mcr.microsoft.com/k8se/quickstart:latest if source control fails the subsequent times by setting the image
                # to the previously deployed image.
                containers_from_existing_container_app = safe_get(existing_container_app, "properties", "template", "containers", default=[])
                if containers_from_existing_container_app is None or len(containers_from_existing_container_app) == 0:
                    raise ValidationError(
                        "The container app '{}' does not have any containers. Please use --image to set the image for the container app".format(
                            self.get_argument_name()))
                if containers_from_existing_container_app[0]["image"] is None:
                    raise ValidationError(
                        "The container app '{}' does not have an image. Please use --image to set the image for the container app while creating the container app.".format(
                            self.get_argument_name()))
                containers = safe_get(self.containerapp_def, "properties", "template", "containers", default=[])
                if containers is None or len(containers) == 0:
                    raise ValidationError(
                        "The container app '{}' does not have any containers. Please use --image to set the image for the container app".format(
                            self.get_argument_name()))
                containers[0]["image"] = containers_from_existing_container_app[0]["image"]

    def _get_containerapp_if_exists(self):
        try:
            return self.client.show(cmd=self.cmd, resource_group_name=self.get_argument_resource_group_name(), name=self.get_argument_name())
        except Exception as e:
            handle_non_404_status_code_exception(e)
            return None

    def _post_process_for_repo(self):
        from ._up_utils import (get_token, _create_github_action)
        app, env = self._construct_app_and_env_for_source_or_repo()
        # Get GitHub access token
        token = get_token(self.cmd, self.get_argument_repo(), self.get_argument_token())
        _create_github_action(app, env, self.get_argument_service_principal_client_id(), self.get_argument_service_principal_client_secret(),
                              self.get_argument_service_principal_tenant_id(), self.get_argument_branch(), token, self.get_argument_repo(), self.get_argument_context_path())
        cache_github_token(self.cmd, token, self.get_argument_repo())
        return self.client.show(cmd=self.cmd, resource_group_name=self.get_argument_resource_group_name(), name=self.get_argument_name())

    def _construct_app_and_env_for_source_or_repo(self):
        from ._up_utils import (ContainerApp, ResourceGroup, ContainerAppEnvironment, _reformat_image, get_token, _has_dockerfile, _get_dockerfile_content, _get_ingress_and_target_port, _get_registry_details, _create_github_action)
        ingress = self.get_argument_ingress()
        target_port = self.get_argument_target_port()
        dockerfile = "Dockerfile"
        token = get_token(self.cmd, self.get_argument_repo(), self.get_argument_token())

        # Parse resource group name and managed env name
        env_id = safe_get(self.containerapp_def, "properties", "environmentId")
        parsed_env = parse_resource_id(env_id)
        if parsed_env.get('resource_type').lower() == CONNECTED_ENVIRONMENT_RESOURCE_TYPE.lower():
            raise ValidationError(
                "Usage error: --source or --repo cannot be used with a connected environment. Please provide a managed environment instead.")
        env_name = parsed_env['name']
        env_rg = parsed_env['resource_group']

        # Parse location
        env_info = self.get_environment_client().show(cmd=self.cmd, resource_group_name=env_rg, name=env_name)
        location = self.containerapp_def["location"] if "location" in self.containerapp_def else env_info['location']

        # Set image to None if it was previously set to the default image (case where image was not provided by the user) else reformat it
        image = None if self.get_argument_image().__eq__(HELLO_WORLD_IMAGE) else _reformat_image(self.get_argument_source(), self.get_argument_repo(), self.get_argument_image())

        has_dockerfile = _has_dockerfile(self.get_argument_source(), dockerfile)
        if not self.get_argument_source() or has_dockerfile:
            dockerfile_content = _get_dockerfile_content(self.get_argument_repo(), self.get_argument_branch(), token, self.get_argument_source(), self.get_argument_context_path(), dockerfile)
            ingress, target_port = _get_ingress_and_target_port(self.get_argument_ingress(), self.get_argument_target_port(), dockerfile_content)

        # Construct ContainerApp
        resource_group = ResourceGroup(self.cmd, self.get_argument_resource_group_name(), location=location)
        env_resource_group = ResourceGroup(self.cmd, env_rg, location=location)
        env = ContainerAppEnvironment(self.cmd, env_name, env_resource_group, location=location)
        app = ContainerApp(self.cmd, self.get_argument_name(), resource_group, None, image, env, target_port, self.get_argument_registry_server(), self.get_argument_registry_user(), self.get_argument_registry_pass(), self.get_argument_env_vars(), self.get_argument_workload_profile_name(), ingress)

        # Fetch registry credentials
        _get_registry_details(self.cmd, app, self.get_argument_source())  # fetch ACR creds from arguments registry arguments

        return app, env

    def post_process(self, r):
        if is_registry_msi_system(self.get_argument_registry_identity()):
            r = self.create()

        if "properties" in r and "provisioningState" in r["properties"] and r["properties"]["provisioningState"].lower() == "waiting" and not self.get_argument_no_wait():
            not self.get_argument_disable_warnings() and logger.warning('Containerapp creation in progress. Please monitor the creation using `az containerapp show -n {} -g {}`'.format(self.get_argument_name(), self.get_argument_resource_group_name()))

        if "configuration" in r["properties"] and "ingress" in r["properties"]["configuration"] and \
                r["properties"]["configuration"]["ingress"] and "fqdn" in r["properties"]["configuration"]["ingress"]:
            not self.get_argument_disable_warnings() and logger.warning("\nContainer app created. Access your app at https://{}/\n".format(r["properties"]["configuration"]["ingress"]["fqdn"]))
        else:
            target_port = self.get_argument_target_port() or "<port>"
            not self.get_argument_disable_warnings() and logger.warning(
                "\nContainer app created. To access it over HTTPS, enable ingress: "
                "az containerapp ingress enable -n %s -g %s --type external --target-port %s"
                " --transport auto\n", self.get_argument_name(), self.get_argument_resource_group_name(), target_port)

        if self.get_argument_service_connectors_def_list() is not None:
            linker_client = get_linker_client(self.cmd)

            for item in self.get_argument_service_connectors_def_list():
                while r is not None and r["properties"]["provisioningState"].lower() == "inprogress":
                    r = self.client.show(self.cmd, self.get_argument_resource_group_name(), self.get_argument_name())
                    time.sleep(1)
                linker_client.linker.begin_create_or_update(resource_uri=r["id"],
                                                            parameters=item["parameters"],
                                                            linker_name=item["linker_name"]).result()
        if self.get_argument_repo():
            r = self._post_process_for_repo()

        return r

    def set_up_extended_location(self):
        if self.get_argument_environment_type() == CONNECTED_ENVIRONMENT_TYPE:
            if not self.containerapp_def.get('extendedLocation'):
                env_id = safe_get(self.containerapp_def, "properties", 'environmentId') or self.get_argument_managed_env()
                parsed_env = parse_resource_id(env_id)
                env_name = parsed_env['name']
                env_rg = parsed_env['resource_group']
                env_info = self.get_environment_client().show(cmd=self.cmd, resource_group_name=env_rg, name=env_name)
                self.containerapp_def["extendedLocation"] = env_info["extendedLocation"]

    def set_up_service_binds(self):
        if self.get_argument_service_bindings() is not None:
            service_connectors_def_list, service_bindings_def_list = parse_service_bindings(self.cmd,
                                                                                            self.get_argument_service_bindings(),
                                                                                            self.get_argument_resource_group_name(),
                                                                                            self.get_argument_name())
            self.set_argument_service_connectors_def_list(service_connectors_def_list)
            unique_bindings = check_unique_bindings(self.cmd, service_connectors_def_list, service_bindings_def_list,
                                                    self.get_argument_resource_group_name(), self.get_argument_name())
            if not unique_bindings:
                raise ValidationError("Binding names across managed and dev services should be unique.")
            safe_set(self.containerapp_def, "properties", "template", "serviceBinds", value=service_bindings_def_list)

    def get_environment_client(self):
        if self.get_argument_yaml():
            env = safe_get(self.containerapp_def, "properties", "environmentId")
        else:
            env = self.get_argument_managed_env()

        environment_type = self.get_argument_environment_type()
        if not env and not environment_type:
            return ManagedEnvironmentClient

        parsed_env = parse_resource_id(env)

        # Validate environment type
        if parsed_env.get('resource_type').lower() == CONNECTED_ENVIRONMENT_RESOURCE_TYPE.lower():
            if environment_type == MANAGED_ENVIRONMENT_TYPE:
                logger.warning("User passed a connectedEnvironment resource id but did not specify --environment-type connected. Using environment type connected.")
            environment_type = CONNECTED_ENVIRONMENT_TYPE
        else:
            if environment_type == CONNECTED_ENVIRONMENT_TYPE:
                logger.warning("User passed a managedEnvironment resource id but specified --environment-type connected. Using environment type managed.")
            environment_type = MANAGED_ENVIRONMENT_TYPE

        self.set_argument_environment_type(environment_type)
        self.set_argument_managed_env(env)

        if environment_type == CONNECTED_ENVIRONMENT_TYPE:
            return ConnectedEnvironmentClient
        else:
            return ManagedEnvironmentPreviewClient

    def get_argument_environment_type(self):
        return self.get_param("environment_type")

    def set_argument_environment_type(self, environment_type):
        self.set_param("environment_type", environment_type)

    def get_argument_source(self):
        return self.get_param("source")

    def get_argument_repo(self):
        return self.get_param("repo")

    def get_argument_branch(self):
        return self.get_param("branch")

    def get_argument_token(self):
        return self.get_param("token")

    def get_argument_context_path(self):
        return self.get_param("context_path")

    def get_argument_service_principal_client_id(self):
        return self.get_param("service_principal_client_id")

    def get_argument_service_principal_client_secret(self):
        return self.get_param("service_principal_client_secret")

    def get_argument_service_principal_tenant_id(self):
        return self.get_param("service_principal_tenant_id")


# decorator for preview update
class ContainerAppPreviewUpdateDecorator(ContainerAppUpdateDecorator):
    def construct_payload(self):
        super().construct_payload()
        self.set_up_service_bindings()
        self.set_up_unbind_service_bindings()
        self.set_up_source()

    def get_argument_source(self):
        return self.get_param("source")

    def set_up_source(self):
        if self.get_argument_source():
            if self.get_argument_yaml():
                raise MutuallyExclusiveArgumentError("Cannot use --source with --yaml together. Can either deploy from a local directory or provide a yaml file")
            # Check if an ACR registry is associated with the container app
            existing_registries = safe_get(self.containerapp_def, "properties", "configuration", "registries", default=[])
            existing_registries = [r for r in existing_registries if ACR_IMAGE_SUFFIX in r["server"]]
            if existing_registries is None or len(existing_registries) == 0:
                raise ValidationError(
                    "Error: The containerapp '{}' does not have an ACR registry associated with it. Please specify a registry using the --registry-server argument while creating the ContainerApp".format(
                        self.get_argument_name()))
            registry_server = existing_registries[0]["server"]
            parsed = urlparse(registry_server)
            registry_name = (parsed.netloc if parsed.scheme else parsed.path).split('.')[0]
            registry_user, registry_pass, _ = _get_acr_cred(self.cmd.cli_ctx, registry_name)
            self._update_container_app_source(cmd=self.cmd, registry_server=registry_server, registry_user=registry_user, registry_pass=registry_pass)

    def _update_container_app_source(self, cmd, registry_server, registry_user, registry_pass):
        from ._up_utils import (ContainerApp, ResourceGroup, ContainerAppEnvironment, _reformat_image, _has_dockerfile, _get_dockerfile_content, _get_ingress_and_target_port, _get_registry_details)

        ingress = self.get_argument_ingress()
        target_port = self.get_argument_target_port()
        dockerfile = "Dockerfile"

        # Parse resource group name and managed env name
        env_id = safe_get(self.containerapp_def, "properties", "environmentId")
        parsed_env = parse_resource_id(env_id)
        if parsed_env.get('resource_type').lower() == CONNECTED_ENVIRONMENT_RESOURCE_TYPE.lower():
            raise ValidationError(
                "Usage error: --source cannot be used with a connected environment. Please provide a managed environment instead.")
        env_name = parsed_env['name']
        env_rg = parsed_env['resource_group']

        # Set image to None if it was previously set to the default image (case where image was not provided by the user) else reformat it
        image = None if self.get_argument_image() is None else _reformat_image(source=self.get_argument_source(), image=self.get_argument_image(), repo=None)

        # Parse location
        env_info = self.get_environment_client().show(cmd=self.cmd, resource_group_name=env_rg, name=env_name)
        location = self.containerapp_def["location"] if "location" in self.containerapp_def else env_info['location']

        has_dockerfile = _has_dockerfile(self.get_argument_source(), dockerfile)
        if has_dockerfile:
            dockerfile_content = _get_dockerfile_content(repo=None, branch=None, token=None, source=self.get_argument_source(), context_path=None, dockerfile=dockerfile)
            ingress, target_port = _get_ingress_and_target_port(self.get_argument_ingress(), self.get_argument_target_port(), dockerfile_content)

        # Construct ContainerApp
        resource_group = ResourceGroup(cmd, self.get_argument_resource_group_name(), location=location)
        env_resource_group = ResourceGroup(cmd, env_rg, location=location)
        env = ContainerAppEnvironment(cmd, env_name, env_resource_group, location=location)
        app = ContainerApp(cmd=cmd, name=self.get_argument_name(), resource_group=resource_group, image=image, env=env, target_port=target_port, workload_profile_name=self.get_argument_workload_profile_name(), ingress=ingress, registry_server=registry_server, registry_user=registry_user, registry_pass=registry_pass)

        # Fetch registry credentials
        _get_registry_details(cmd, app, self.get_argument_source())  # fetch ACR creds from arguments registry arguments

        # Uses buildpacks or an ACR Task to generate image if Dockerfile was not provided by the user
        app.run_acr_build(dockerfile, self.get_argument_source(), quiet=False, build_from_source=not has_dockerfile)

        # Validate an image associated with the container app exists
        containers = safe_get(self.containerapp_def, "properties", "template", "containers", default=[])
        if containers is None or len(containers) == 0:
            raise ValidationError(
                "The container app '{}' does not have any containers. Please use --image to set the image for the container app".format(
                    self.get_argument_name()))
        self.new_containerapp["properties"]["template"] = {} if "template" not in self.new_containerapp["properties"] else self.new_containerapp["properties"]["template"]
        self.new_containerapp["properties"]["template"]["containers"] = containers
        # Update image in the container app
        self.new_containerapp["properties"]["template"]["containers"][0]["image"] = HELLO_WORLD_IMAGE if app.image is None else app.image

    def post_process(self, r):
        # Delete managed bindings
        linker_client = None
        if self.get_argument_unbind_service_bindings():
            linker_client = get_linker_client(self.cmd)
            for item in self.get_argument_unbind_service_bindings():
                while r["properties"]["provisioningState"].lower() == "inprogress":
                    r = self.client.show(self.cmd, self.get_argument_resource_group_name(), self.get_argument_name())
                    time.sleep(1)
                linker_client.linker.begin_delete(resource_uri=r["id"], linker_name=item).result()

        # Update managed bindings
        if self.get_argument_service_connectors_def_list() is not None:
            linker_client = get_linker_client(self.cmd) if linker_client is None else linker_client
            for item in self.get_argument_service_connectors_def_list():
                while r["properties"]["provisioningState"].lower() == "inprogress":
                    r = self.client.show(self.cmd, self.get_argument_resource_group_name(), self.get_argument_name())
                    time.sleep(1)
                linker_client.linker.begin_create_or_update(resource_uri=r["id"],
                                                            parameters=item["parameters"],
                                                            linker_name=item["linker_name"]).result()
        return r

    def set_up_service_bindings(self):
        if self.get_argument_service_bindings() is not None:
            linker_client = get_linker_client(self.cmd)

            service_connectors_def_list, service_bindings_def_list = parse_service_bindings(self.cmd, self.get_argument_service_bindings(), self.get_argument_resource_group_name(), self.get_argument_name())
            self.set_argument_service_connectors_def_list(service_connectors_def_list)
            service_bindings_used_map = {update_item["name"]: False for update_item in service_bindings_def_list}

            safe_set(self.new_containerapp, "properties", "template", "serviceBinds", value=self.containerapp_def["properties"]["template"]["serviceBinds"])

            if self.new_containerapp["properties"]["template"]["serviceBinds"] is None:
                self.new_containerapp["properties"]["template"]["serviceBinds"] = []

            for item in self.new_containerapp["properties"]["template"]["serviceBinds"]:
                for update_item in service_bindings_def_list:
                    if update_item["name"] in item.values():
                        item["serviceId"] = update_item["serviceId"]
                        service_bindings_used_map[update_item["name"]] = True

            for update_item in service_bindings_def_list:
                if service_bindings_used_map[update_item["name"]] is False:
                    # Check if it doesn't exist in existing service linkers
                    managed_bindings = linker_client.linker.list(resource_uri=self.containerapp_def["id"])
                    if managed_bindings:
                        managed_bindings_list = [item.name for item in managed_bindings]
                        if update_item["name"] in managed_bindings_list:
                            raise ValidationError("Binding names across managed and dev services should be unique.")

                    self.new_containerapp["properties"]["template"]["serviceBinds"].append(update_item)

            if service_connectors_def_list is not None:
                for item in service_connectors_def_list:
                    # Check if it doesn't exist in existing service bindings
                    service_bindings_list = []
                    for binds in self.new_containerapp["properties"]["template"]["serviceBinds"]:
                        service_bindings_list.append(binds["name"])
                        if item["linker_name"] in service_bindings_list:
                            raise ValidationError("Binding names across managed and dev services should be unique.")

    def set_up_unbind_service_bindings(self):
        if self.get_argument_unbind_service_bindings():
            new_template = self.new_containerapp.setdefault("properties", {}).setdefault("template", {})
            existing_template = self.containerapp_def["properties"]["template"]

            if not self.get_argument_service_bindings():
                new_template["serviceBinds"] = existing_template.get("serviceBinds", [])

            service_bindings_dict = {}
            if new_template["serviceBinds"]:
                service_bindings_dict = {service_binding["name"]: index for index, service_binding in
                                         enumerate(new_template.get("serviceBinds", []))}

            for item in self.get_argument_unbind_service_bindings():
                if item in service_bindings_dict:
                    new_template["serviceBinds"] = [binding for binding in new_template["serviceBinds"] if
                                                    binding["name"] != item]


# decorator for preview list
class ContainerAppPreviewListDecorator(BaseContainerAppDecorator):
    def __init__(
        self, cmd: AzCliCommand, client: Any, raw_parameters: Dict, models: str
    ):
        super().__init__(cmd, client, raw_parameters, models)

    def list(self):
        containerapps = super().list()
        if self.get_argument_environment_type() == CONNECTED_ENVIRONMENT_TYPE:
            containerapps = [c for c in containerapps if CONNECTED_ENVIRONMENT_RESOURCE_TYPE in c["properties"]["environmentId"]]
        if self.get_argument_environment_type() == MANAGED_ENVIRONMENT_TYPE:
            containerapps = [c for c in containerapps if MANAGED_ENVIRONMENT_RESOURCE_TYPE in c["properties"]["environmentId"]]
        return containerapps

    def get_environment_client(self):
        env = self.get_argument_managed_env()

        if is_valid_resource_id(env):
            parsed_env = parse_resource_id(env)
            if parsed_env.get('resource_type').lower() == CONNECTED_ENVIRONMENT_RESOURCE_TYPE.lower():
                return ConnectedEnvironmentClient
            else:
                return ManagedEnvironmentPreviewClient

        if self.get_argument_environment_type() == CONNECTED_ENVIRONMENT_TYPE:
            return ConnectedEnvironmentClient
        else:
            return ManagedEnvironmentPreviewClient

    def get_argument_environment_type(self):
        return self.get_param("environment_type")
