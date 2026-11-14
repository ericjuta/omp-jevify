**P2: Makes one more request than configured**

With the default of three attempts this helper sends four requests, because the loop bound is inclusive. Callers that budget three calls per operation will exceed their rate limit. Use a strict bound so the configured count is the number of requests.

Useful? React with 👍 / 👎.
