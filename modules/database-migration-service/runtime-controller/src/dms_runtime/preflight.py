import json
import time
from collections.abc import Callable
from typing import Any


class SourcePreflight:
    def __init__(
        self,
        dms_client: Any,
        lambda_client: Any,
        *,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.dms = dms_client
        self.lambda_client = lambda_client
        self.sleep = sleep

    def run(
        self,
        *,
        replication_instance_arn: str,
        source_endpoint_arn: str,
        credential_sync_function_arn: str | None = None,
        max_poll_attempts: int = 30,
        poll_interval_seconds: int = 10,
    ) -> dict[str, Any]:
        for name, value in (
            ("replication_instance_arn", replication_instance_arn),
            ("source_endpoint_arn", source_endpoint_arn),
        ):
            if (
                not isinstance(value, str)
                or not value
                or value != value.strip()
            ):
                raise ValueError(
                    f"{name} must be a non-empty trimmed string"
                )

        if credential_sync_function_arn is not None and (
            not isinstance(credential_sync_function_arn, str)
            or not credential_sync_function_arn
            or credential_sync_function_arn
            != credential_sync_function_arn.strip()
        ):
            raise ValueError(
                "credential_sync_function_arn must be "
                "a non-empty trimmed string"
            )

        for name, value, maximum in (
            ("max_poll_attempts", max_poll_attempts, 30),
            ("poll_interval_seconds", poll_interval_seconds, 10),
        ):
            if (
                not isinstance(value, int)
                or isinstance(value, bool)
                or not 1 <= value <= maximum
            ):
                raise ValueError(
                    f"{name} must be an integer from 1 to {maximum}"
                )

        synchronised = self._synchronise_credentials(
            credential_sync_function_arn
        )

        self.dms.test_connection(
            ReplicationInstanceArn=replication_instance_arn,
            EndpointArn=source_endpoint_arn,
        )

        for attempt in range(1, max_poll_attempts + 1):
            response = self.dms.describe_connections(
                Filters=[
                    {
                        "Name": "endpoint-arn",
                        "Values": [source_endpoint_arn],
                    },
                    {
                        "Name": "replication-instance-arn",
                        "Values": [replication_instance_arn],
                    },
                ]
            )

            connections = response.get("Connections", [])

            if len(connections) != 1 or response.get("Marker"):
                raise RuntimeError(
                    "DMS must return exactly one source connection"
                )

            connection = connections[0]

            if (
                connection.get("EndpointArn") != source_endpoint_arn
                or connection.get("ReplicationInstanceArn")
                != replication_instance_arn
            ):
                raise RuntimeError(
                    "DMS returned a different source connection"
                )

            status = connection.get("Status")

            if status == "successful":
                return {
                    "credentialsSynchronised": synchronised,
                    "sourceEndpointArn": source_endpoint_arn,
                    "replicationInstanceArn": replication_instance_arn,
                    "connectionStatus": status,
                    "pollAttempts": attempt,
                }

            if status == "failed":
                raise RuntimeError(
                    "DMS source endpoint connection test failed"
                )

            if status != "testing":
                raise RuntimeError(
                    f"Unexpected DMS connection status: {status!r}"
                )

            if attempt < max_poll_attempts:
                self.sleep(poll_interval_seconds)

        raise TimeoutError(
            "Timed out waiting for the DMS source connection test"
        )

    def _synchronise_credentials(
        self,
        function_arn: str | None,
    ) -> bool:
        if function_arn is None:
            return False

        response = self.lambda_client.invoke(
            FunctionName=function_arn,
            InvocationType="RequestResponse",
            Payload=json.dumps(
                {"trigger": "preflight"}
            ).encode("utf-8"),
        )

        stream = response["Payload"]

        try:
            if response.get("FunctionError"):
                raise RuntimeError(
                    "DMS source credential synchronisation failed"
                )

            try:
                payload = json.loads(stream.read() or b"{}")
            except (ValueError, UnicodeError) as error:
                raise RuntimeError(
                    "Credential synchronisation returned invalid JSON"
                ) from error

        finally:
            stream.close()

        if (
            not isinstance(payload, dict)
            or payload.get("statusCode") != 200
        ):
            raise RuntimeError(
                "Credential synchronisation returned "
                "an unsuccessful response"
            )

        return True