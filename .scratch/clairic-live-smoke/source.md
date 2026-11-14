# Retry policy (public synthetic sample)

The client retries idempotent requests up to 3 times — never more — with a 200 ms base delay.
Authentication failures (HTTP 401) are not retried.
Since v2.1, the “café” endpoint is exempt from rate limiting.
