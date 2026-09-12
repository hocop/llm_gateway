I want to make a gateway that:
- routes traffic to openai-compatible endpoints, can rename the models and route between them
- provides virtual keys, configurable through toml
- limits the model usage by concurrency instead of rpm/tpm

# Model routing
For example, we can have:

`providers.toml`:

```toml
[[providers]]
name = llama_cpp
url = http://localhost:8080/v1
key = ""  # unneeded

[[providers]]
name = vllm
url = http://localhost:8000/v1
key = "my-secret-key"
```

And the set of "virtual" models, routing to real models:

`virtual_models.toml`:

```toml
[[virtual_models]]
name = fast_model
model = vllm/qwen3.8_27B-fp8
description = "Fast model for everyday use"

[[virtual_models]]
name = smart_model
model = llama_cpp/qwen3.8_flash_next-GGUF
description = "Smart model for challenging tasks"

[[virtual_models]]
name = first_available
model = ["/fast_model", "/smart_model"]  # no provider means this gateway
description = "Will route to fast by default, and to slow if fast is not available"
```

The gateway exposes an openai-compatible API. When user requests `/v1/models`, they get the only list of virtual models.

Real models can be unavailable sometimes. Service should manage that gracefully. If a list is provided as `model`, is means try them consecutively. If model name is written without provider, such as `/fast_model`, it means a reference to a virtual models. Those references must be checked as service startup.

Load-balancing between providers/models is not the scope of this software and will not be implemented.
# Virtual keys with quota
This is the main goal of this gateway.

`virtual_keys.toml`:

```toml
[[virtual_keys]]
name = me
models = '*'  # wildcards supported
quotas = [
	{
		model = 'fast_model',
		max_concurrency = 2,
	},
	{
		model = 'smart_model',
		max_concurrency = 1,
	}
]

[[virtual_keys]]
name = my_friend
models = ['fast_model', 'smart_model']
quotas = [
	{
		model = 'fast_model',
		max_concurrency = 2,
	},
	{
		model = 'smart_model',
		max_concurrency = 0.5,
	}
]

[[virtual_keys]]
name = my_service_1
models = ['first_available']
quotas = [
	{
		model = 'fast_model',
		max_concurrency = 1.5,
	},
	{
		model = 'smart_model',
		max_concurrency = 0.5,
	}
]
```

The provider is not written here, as models here are only virtual.

The `key_for_my_service_1` example features quotas for proxied models - these must also work.

The keys themselves are stored using env variables like: `LLM_KEY_MY_SERVICE_1`. Names are not case-sensitive and must all be lowercase in the config.
### How max_concurrency works
First mental model, then actual implementation.

**Mental model.**

Each virtual key has its own input queue and `ceil(max_concurrency)` virtual workers. Each worker has its own capacity $\alpha_i \in [0, 1]$ such that  $\sum\alpha_i=maxcapacity$ . For example, with `max_capacity = 2.5` we have two workers with $\alpha=1$ and one with $\alpha=0.5$.

Each worker does one thing:
```python
async def work(queue: asyncio.Queue, capacity: float):
	while True:
		request = await queue.get()
		start = time.time()
		result = await call_llm(request)
		elapsed = time.time() - start
		residual_time = elapsed * (1 - capacity) / capacity
		await asyncio.sleep(residual_time)
```

`sleep(residual_time)` is the main mechanic here. With `capacity=1`, we have no timeout between requests. With `capacity=0.5`, timeout equals the time the request was processing. With `capacity=0.1`, timeout is 9 times more, than request processing time.

**Actual implementation.**

While mental model is simple, we don't want to spawn a lot of workers doing nothing. It also doesn't work for unlimited capacity. Failsafeness is another concern.

Better implementation is through dynamic variable, stored in external cache (redis/valkey). Each virtual key has variable called `current_requests_running`.
Then for each request we do:

```python
async def process_request(request, key: str, output_queue: asyncio.Queue):
	budget_taken: float
	eps = 0.1  # reasonable
	max_concurrency: float = get_from_config(key, 'max_c')

	# Wait for quota to free up
	is_first_time = True
	while True:
		if not is_first_time:
			# ideally, we listen for a signal here, with timeout
			await asyncio.sleep(0.2)
		async with mutex_lock_variable(key, 'current') as current:
			current_requests_running = current.get()
			budget = max_concurrency - current_requests_running
			if budget < eps:
				continue

			budget_taken = min(1, budget)
			current.set(current_requests_running + budget_taken)

	# Process the request
	start = time.time()
	result = await call_llm(request)
	await output_queue.put(result)  # this one is newly added. No need for client to wait on result, only on the quota for the next request.
	elapsed = time.time() - start

	# Sleep the residual before giving the capacity back
	capacity = budget_taken  # from 0 to 1
	residual_time = elapsed * (1 - capacity) / capacity
	await asyncio.sleep(residual_time)

	async with mutex_lock_variable(key, 'current') as current:
		current_requests_running = current.get()
		current.set(current_requests_running - budget_taken)
```

Above is pseudo-code. The mutex should be better abstracted. The waiting for quota should be decoupled into a different class. Maybe the whole waiting logic can be separated into a `async with Quota(key):` context.

If the code fails before giving back the capacity, the service may break. Might add some hard-resets on a cache layer with some reasonable TTL, on top of try/except. Addition and subtraction operations are not exact and may diverge over time - should also be addressed. Maybe each request with its `budget_taken` should be stored in the cache (with TTL) and the dynamic variable can be computed each time from scratch. More requests, but robustness is more important.

## Other requirements

As this is an LLM router, typical RPS values are low (1-20) with occasional bursts amortized by gateway-level timeouts. Robustness and failsafe are top priority. The service needs to be scalable by design - meaning that all dynamic variables must be in the cache layer (valkey), not in RAM. Scalability is more for seamless deployment than for high resource consumption.

Service must work with llama-cpp and vllm endpoints. It must work with streaming responses and multimodal inputs. The additional response fields such as token usage must be preserved. In the `/v1/models` responses, we must list all the (virtual) models available to the provided key along with descriptions and quotas. It must also support embedding models and other less used endpoints.

## Implementation requirements

Service must be implemented using python3.12 with modern typing style. Using fastapi as backend framework. Using valkey credentials given in the env variables. All usage paths must be covered by unit tests with mocked llm endpoints. Must use `uv` for dependency management and only it (no manual `uv pip install` in the venv, only `uv add`). Use `ty` for type checking.
