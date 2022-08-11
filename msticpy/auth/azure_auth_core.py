# -------------------------------------------------------------------------
# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License. See License.txt in the project root for
# license information.
# --------------------------------------------------------------------------
"""Azure KeyVault pre-authentication."""

import contextlib
import logging
import sys
from collections import namedtuple
from datetime import datetime
from enum import Enum
from typing import List, Optional, Tuple, Union

from azure.common.credentials import get_cli_profile
from azure.common.exceptions import CloudError
from azure.identity import (
    AzureCliCredential,
    AzurePowerShellCredential,
    CertificateCredential,
    ChainedTokenCredential,
    ClientSecretCredential,
    DeviceCodeCredential,
    EnvironmentCredential,
    InteractiveBrowserCredential,
    ManagedIdentityCredential,
    VisualStudioCodeCredential,
)
from dateutil import parser
from msrestazure import azure_cloud

from .._version import VERSION
from ..common import pkg_config as config
from ..common.exceptions import MsticpyAzureConfigError
from .cloud_mappings import (
    CLOUD_ALIASES,
    CLOUD_MAPPING,
    get_all_endpoints,
    get_all_suffixes,
)
from .cred_wrapper import CredentialWrapper

__version__ = VERSION
__author__ = "Pete Bryan"


AzCredentials = namedtuple("AzCredentials", ["legacy", "modern"])

_EXCLUDED_AUTH = {
    "cli": True,
    "env": True,
    "msi": True,
    "vscode": True,
    "powershell": True,
    "interactive": True,
    "cache": True,
}


def get_azure_config_value(key, default):
    """Get a config value from Azure section."""
    with contextlib.suppress(KeyError):
        az_settings = config.get_config("Azure")
        if az_settings and key in az_settings:
            return az_settings[key]
    return default


def default_auth_methods() -> List[str]:
    """Get the default (all) authentication options."""
    return get_azure_config_value("auth_methods", ["cli", "msi", "interactive"])


class AzureCloudConfig:
    """Azure Cloud configuration."""

    def __init__(self, cloud: str = None, tenant_id: Optional[str] = None):
        """
        Initialize AzureCloudConfig from `cloud` or configuration.

        Parameters
        ----------
        cloud : str, optional
            The cloud to retrieve configuration for. If not supplied,
            the cloud ID is read from configuration. If this is not available,
            it defaults to 'global'.
        tenant_id : str, optional
            The tenant to authenticate against. If not supplied,
            the tenant ID is read from configuration, or the default tenant
            for the identity.

        """
        self.cloud = cloud or get_azure_config_value("cloud", "global")
        self.tenant_id = tenant_id or get_azure_config_value("tenant_id", None)
        self.auth_methods = default_auth_methods()

    @property
    def cloud_names(self) -> List[str]:
        """Return a list of current cloud names."""
        return list(CLOUD_MAPPING.keys())

    @staticmethod
    def resolve_cloud_alias(alias) -> Optional[str]:
        """Return match of cloud alias or name."""
        alias_cf = alias.casefold()
        aliases = {alias.casefold(): cloud for alias, cloud in CLOUD_ALIASES.items()}
        if alias_cf in aliases:
            return aliases[alias_cf]
        if alias_cf in aliases.values():
            return alias_cf
        return None

    @property
    def endpoints(self) -> azure_cloud.CloudEndpoints:
        """
        Get a list of all the endpoints for an Azure cloud.

        Returns
        -------
        dict
            A dictionary of endpoints for the cloud.

        Raises
        ------
        MsticpyAzureConfigError
            If the cloud name is not valid.

        """
        return get_all_endpoints(self.cloud)

    @property
    def suffixes(self) -> azure_cloud.CloudSuffixes:
        """
        Get a list of all the suffixes for an Azure cloud.

        Returns
        -------
        dict
            A dictionary of suffixes for the cloud.

        Raises
        ------
        MsticpyAzureConfigError
            If the cloud name is not valid.

        """
        return get_all_suffixes(self.cloud)

    @property
    def token_uri(self) -> str:
        """Return the resource manager token URI."""
        return f"{self.endpoints.resource_manager}.default"


def _build_env_client(
    tenant_id: str = None, aad_uri: str = None, **kwargs
) -> EnvironmentCredential:
    """Build a credential from environment variables."""
    del kwargs
    return EnvironmentCredential(tenant_id=tenant_id, authority=aad_uri)  # type: ignore


def _build_cli_client(**kwargs) -> AzureCliCredential:
    """Build a credential from Azure CLI."""
    del kwargs
    return AzureCliCredential()


def _build_msi_client(
    tenant_id: str = None, aad_uri: str = None, **kwargs
) -> ManagedIdentityCredential:
    """Build a credential from Managed Identity."""
    del kwargs
    return ManagedIdentityCredential(tenant_id=tenant_id, authority=aad_uri)


def _build_vscode_client(
    tenant_id: str = None, aad_uri: str = None, **kwargs
) -> VisualStudioCodeCredential:
    """Build a credential from Visual Studio Code."""
    del kwargs
    return VisualStudioCodeCredential(authority=aad_uri, tenant_id=tenant_id)


def _build_interactive_client(
    tenant_id: str = None, aad_uri: str = None, **kwargs
) -> InteractiveBrowserCredential:
    """Build a credential from Interactive Browser logon."""
    return InteractiveBrowserCredential(
        authority=aad_uri, tenant_id=tenant_id, **kwargs
    )


def _build_device_code_client(
    tenant_id: str = None, aad_uri: str = None, **kwargs
) -> DeviceCodeCredential:
    """Build a credential from Device Code."""
    return DeviceCodeCredential(authority=aad_uri, tenant_id=tenant_id, **kwargs)


def _build_client_secret_client(
    tenant_id: str = None, aad_uri: str = None, **kwargs
) -> ClientSecretCredential:
    """Build a credential from Client Secret."""
    client_id = kwargs.pop("client_id", None)
    client_secret = kwargs.pop("client_secret", None)
    if not client_secret or not client_id:
        raise MsticpyAzureConfigError("Client secret and client id are required.")
    return ClientSecretCredential(
        authority=aad_uri,
        tenant_id=tenant_id,  # type: ignore
        client_id=client_id,
        client_secret=client_secret,
        **kwargs,
    )


def _build_certificate_client(
    tenant_id: str = None, aad_uri: str = None, **kwargs
) -> CertificateCredential:
    """Build a credential from Certificate."""
    client_id = kwargs.pop("client_id", None)
    if not client_id:
        raise MsticpyAzureConfigError("Client id required.")
    return CertificateCredential(
        authority=aad_uri, tenant_id=tenant_id, client_id=client_id, **kwargs  # type: ignore
    )


def _build_powershell_client(**kwargs) -> AzurePowerShellCredential:
    """Build a credential from PowerShell logon session."""
    del kwargs
    return AzurePowerShellCredential()


_CLIENTS = dict(
    {
        "env": _build_env_client,
        "cli": _build_cli_client,
        "msi": _build_msi_client,
        "vscode": _build_vscode_client,
        "powershell": _build_powershell_client,
        "interactive": _build_interactive_client,
        "interactive_browser": _build_interactive_client,
        "devicecode": _build_device_code_client,
        "device_code": _build_device_code_client,
        "device": _build_device_code_client,
        "environment": _build_env_client,
        "managedidentity": _build_msi_client,
        "managed_identity": _build_msi_client,
        "clientsecret": _build_client_secret_client,
        "client_secret": _build_client_secret_client,
        "certificate": _build_certificate_client,
        "cert": _build_certificate_client,
    }
)


def _az_connect_core(
    auth_methods: List[str] = None,
    cloud: str = None,
    tenant_id: str = None,
    silent: bool = False,
    **kwargs,
) -> AzCredentials:
    """
    Authenticate using multiple authentication sources.

    Parameters
    ----------
    auth_methods : List[str], optional
        List of authentication methods to try
        Possible options are:
        - "env" - to get authentication details from environment variables
        - "cli" - to use Azure CLI authentication details
        - "msi" - to user Managed Service Identity details
        - "vscode" - to use VSCode credentials
        - "powershell" - to use PowerShell credentials
        - "interactive" - to prompt for interactive login
        - "cache" - to use shared token cache credentials
        If not set, it will use the value defined in msticpyconfig.yaml.
        If this is not set, the default is ["env", "cli", "msi", "interactive"]
    cloud : str, optional
        What Azure cloud to connect to.
        By default it will attempt to use the cloud setting from config file.
        If this is not set it will default to Azure Public Cloud
    tenant_id : str, optional
        The tenant to authenticate against. If not supplied,
        the tenant ID is read from configuration, or the default tenant for the identity.
    silent : bool, optional
        Whether to display any output during auth process. Default is False.

    Returns
    -------
    AzCredentials
                Named tuple of:
        - legacy (ADAL) credentials
        - modern (MSAL) credentials

    Raises
    ------
    CloudError
        If chained token credential creation fails.

    Notes
    -----
    The function tries to obtain credentials from the following
    sources:
    - Azure Auth Environment variables
    - Azure CLI (if an active session is logged on)
    - Managed Service Identity
    - Interactive browser logon
    If the authentication is successful both ADAL (legacy) and
    MSAL (modern) credential types are returned.

    """
    # Create the auth methods with the specified cloud region
    cloud = cloud or kwargs.pop("region", AzureCloudConfig().cloud)
    az_config = AzureCloudConfig(cloud)
    aad_uri = az_config.endpoints.active_directory
    tenant_id = tenant_id or AzureCloudConfig().tenant_id
    creds = _build_chained_creds(
        aad_uri=aad_uri, requested_clients=auth_methods, tenant_id=tenant_id, **kwargs
    )

    # Filter and replace error message when credentials not found
    handler = logging.StreamHandler(sys.stdout)
    if silent:
        handler.addFilter(_filter_all_warnings)
    else:
        handler.addFilter(_filter_credential_warning)
    logging.basicConfig(level=logging.WARNING, handlers=[handler])

    # Connect to the subscription client to validate
    legacy_creds = CredentialWrapper(
        creds, resource_id=AzureCloudConfig(cloud).token_uri
    )
    if not creds:
        raise CloudError("Could not obtain credentials.")

    return AzCredentials(legacy_creds, creds)


def _build_chained_creds(
    aad_uri,
    requested_clients: Union[List[str], None] = None,
    tenant_id: str = None,
    **kwargs,
) -> ChainedTokenCredential:
    """
    Build a chained token credential.

    Parameters
    ----------
    requested_clients : List[str]
        List of clients to chain.
    aad_uri : str
        The URI of the Azure AD cloud to connect to
    tenant_id : str
        The tenant ID to connect to

    Returns
    -------
    ChainedTokenCredential
        A chained token credential.

    Raises
    ------
    CloudError
        If the chained credential creation fails.

    """
    # Create the chained credential
    if not requested_clients:
        requested_clients = ["env", "cli", "msi", "interactive"]
    clients = [
        _CLIENTS[client](tenant_id=tenant_id, aad_uri=aad_uri, **kwargs)  # type: ignore
        for client in requested_clients
    ]
    if not clients:
        raise MsticpyAzureConfigError(
            "At least one valid authentication method required."
        )

    return ChainedTokenCredential(*clients)


class _AzCachedConnect:
    """Singleton class caching Azure credentials."""

    _instance = None

    def __new__(cls):
        """Override new to check and return existing instance."""
        if cls._instance is None:
            cls._instance = super(_AzCachedConnect, cls).__new__(cls)
            cls.connect.__doc__ = _az_connect_core.__doc__
        return cls._instance

    def __init__(self):
        """Initialize the class."""
        self.az_credentials: Optional[AzCredentials] = None
        self.cred_cloud: str = self.current_cloud

    @property
    def current_cloud(self) -> str:
        """Return current cloud."""
        return AzureCloudConfig().cloud

    def connect(self, *args, **kwargs):
        """Call az_connect_core if token is not present or expired."""
        if self.az_credentials is None:
            self.az_credentials = _az_connect_core(*args, **kwargs)
            return self.az_credentials
        # Check expiry
        if (
            datetime.utcfromtimestamp(
                self.az_credentials.modern.get_token(
                    AzureCloudConfig().token_uri
                ).expires_on
            )
            <= datetime.utcnow()
        ):
            self.az_credentials = _az_connect_core(*args, **kwargs)
        # Check changed cloud
        if self.cred_cloud != kwargs.get(
            "cloud", kwargs.get("region", self.current_cloud)
        ):
            self.az_credentials = _az_connect_core(*args, **kwargs)
        return self.az_credentials


# externally callable function using the class above
# _AZ_CACHED_CONNECT = _AzCachedConnect()
az_connect_core = _az_connect_core


def only_interactive_cred(chained_cred: ChainedTokenCredential):
    """Return True if only interactivebrowser credentials available."""
    return len(chained_cred.credentials) == 1 and isinstance(
        chained_cred.credentials[0], InteractiveBrowserCredential
    )


def _filter_credential_warning(record) -> bool:
    """Rewrite out credential not found message."""
    if (
        not record.name.startswith("azure.identity")
        or record.levelno != logging.WARNING
    ):
        return True
    message = record.getMessage()
    if ".get_token" in message:
        if message.startswith("EnvironmentCredential"):
            print("Unable to sign-in with environment variable credentials.")
        if message.startswith("AzureCliCredential"):
            print("Unable to sign-in with Azure CLI credentials.")
        if message.startswith("ManagedIdentityCredential"):
            print("Unable to sign-in with Managed Instance credentials.")
    return not message


def _filter_all_warnings(record) -> bool:
    """Filter out credential error messages."""
    if record.name.startswith("azure.identity") and record.levelno == logging.WARNING:
        message = record.getMessage()
        if ".get_token" in message:
            return not message
    return True


class AzureCliStatus(Enum):
    """Enumeration for _check_cli_credentials return values."""

    CLI_OK = 0
    CLI_NOT_INSTALLED = 1
    CLI_NEEDS_SIGN_IN = 2
    CLI_TOKEN_EXPIRED = 3
    CLI_UNKNOWN_ERROR = 4


def check_cli_credentials() -> Tuple[AzureCliStatus, Optional[str]]:
    """Check to see if there is a CLI session with a valid AAD token."""
    try:
        cli_profile = get_cli_profile()
        raw_token = cli_profile.get_raw_token()
        bearer_token = None
        if (
            isinstance(raw_token, tuple)
            and len(raw_token) == 3
            and len(raw_token[0]) == 3
        ):
            bearer_token = raw_token[0][2]
            if (
                parser.parse(bearer_token.get("expiresOn", datetime.min))
                < datetime.now()
            ):
                raise ValueError("AADSTS70043: The refresh token has expired")

        return AzureCliStatus.CLI_OK, "Azure CLI credentials available."
    except ImportError:
        # Azure CLI not installed
        return AzureCliStatus.CLI_NOT_INSTALLED, None
    except Exception as ex:  # pylint: disable=broad-except
        if "AADSTS70043: The refresh token has expired" in str(ex):
            message = (
                "Azure CLI was detected but the token has expired. "
                "For Azure CLI single sign-on, please sign in using '!az login'."
            )
            return AzureCliStatus.CLI_TOKEN_EXPIRED, message
        if "Please run 'az login' to setup account" in str(ex):
            message = (
                "Azure CLI was detected but no token is available. "
                "For Azure CLI single sign-on, please sign in using '!az login'."
            )
            return AzureCliStatus.CLI_NEEDS_SIGN_IN, message
        return AzureCliStatus.CLI_UNKNOWN_ERROR, None