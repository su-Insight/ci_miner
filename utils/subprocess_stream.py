import logging
import random
import subprocess
import time
from typing import List, Optional


def run_command(cmd: List[str], cwd: Optional[str] = None, max_retries: int = 5) -> Optional[str]:
    """Run a command with streamed output and retry on network-related failures."""
    last_exception = None

    for attempt in range(max_retries + 1):
        try:
            if attempt != 0:
                logging.warning(
                    "Retrying command (%s/%s): %s",
                    attempt + 1,
                    max_retries + 1,
                    " ".join(cmd),
                )

            result_lines = []
            with subprocess.Popen(
                cmd,
                cwd=cwd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                bufsize=1,
                text=True,
                encoding="utf-8",
                errors="replace",
            ) as proc:
                # Stream output to the console while collecting it for later inspection.
                for line in proc.stdout:
                    print(line, end="", flush=True)
                    result_lines.append(line)

                returncode = proc.wait()
                output = "".join(result_lines)
                if returncode == 0:
                    if attempt != 0:
                        logging.warning("Command succeeded after retry %s.", attempt + 1)
                    return output
                raise subprocess.CalledProcessError(returncode, cmd, output=output)

        except subprocess.CalledProcessError as exc:
            last_exception = exc
            output = exc.output if hasattr(exc, "output") else str(exc)

            if is_network_error(output):
                logging.warning(
                    "Network-related command failure (%s/%s): %s",
                    attempt + 1,
                    max_retries + 1,
                    get_network_error_summary(output),
                )
                if attempt < max_retries:
                    wait_time = calculate_backoff(attempt)
                    logging.warning("Waiting %.2f seconds before retry.", wait_time)
                    time.sleep(wait_time)
                    continue
                logging.warning("Reached maximum retry count: %s", max_retries)
                break

            logging.warning("Command failed: %s", " ".join(cmd))
            logging.warning("Failure output: %s", output)
            break

        except Exception as exc:
            last_exception = exc
            logging.warning(
                "Unexpected command error (%s/%s): %s",
                attempt + 1,
                max_retries + 1,
                exc,
            )

            if attempt < max_retries and is_network_related_exception(exc):
                wait_time = calculate_backoff(attempt)
                logging.warning("Waiting %.2f seconds before retry.", wait_time)
                time.sleep(wait_time)
                continue

            logging.warning("Unexpected error without retry: %s", exc)
            break

    if last_exception:
        if isinstance(last_exception, subprocess.CalledProcessError):
            logging.warning("Command failed permanently: %s", " ".join(cmd))
            logging.warning(
                "Final output: %s",
                last_exception.output if hasattr(last_exception, "output") else str(last_exception),
            )
        else:
            logging.warning("Final error: %s", last_exception)

    return None


def is_network_error(output: str) -> bool:
    """Return True when command output matches known network failure patterns."""
    network_error_patterns = [
        "connection timed out",
        "failed to connect",
        "could not resolve host",
        "network is unreachable",
        "operation timed out",
        "temporary failure in name resolution",
        "early eof",
        "the remote end hung up unexpectedly",
        "http error",
        "ssl error",
        "certificate problem",
        "timeout",
        "408 request timeout",
        "429 too many requests",
        "500 internal server error",
        "502 bad gateway",
        "503 service unavailable",
        "ssl_error_syscall",
        "504 gateway timeout",
        "fetch-pack: unexpected disconnect",
        "remote hung up",
        "rpc failed",
        "recv failure: connection was reset",
    ]
    output_lower = output.lower()
    return any(pattern in output_lower for pattern in network_error_patterns)


def is_network_related_exception(exception: Exception) -> bool:
    """Return True when an exception looks network-related."""
    exception_str = str(exception).lower()
    network_exception_patterns = ["timeout", "connection", "network", "socket", "http", "ssl"]
    return any(pattern in exception_str for pattern in network_exception_patterns)


def get_network_error_summary(output: str) -> str:
    """Extract a compact network error summary from command output."""
    lines = output.split("\n")
    for line in lines:
        line_lower = line.lower()
        if any(pattern in line_lower for pattern in ["timeout", "connection", "resolve", "unreachable", "hung up", "eof"]):
            return line.strip()
    return "Unknown network error"


def calculate_backoff(attempt: int, base_delay: float = 2.0, max_delay: float = 60.0) -> float:
    """Return an exponential backoff delay with jitter."""
    delay = min(base_delay * (2 ** attempt), max_delay)
    jitter = random.uniform(0.8, 1.2)
    return delay * jitter
