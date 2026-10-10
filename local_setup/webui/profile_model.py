"""Per-task pricing and OpenAI-compatible tool handling, without global price mutation."""

import copy
import math
import os
import time

os.environ.setdefault('LITELLM_LOCAL_MODEL_COST_MAP', 'True')

from minisweagent.models.litellm_model import LitellmModel
from minisweagent.models.litellm_response_model import LitellmResponseModel

from .common import now, redact


def protocol_parameters(snapshot, output_limit=None):
    params = copy.deepcopy(snapshot['parameters'])
    if snapshot['protocol'] == 'responses':
        for name in ['max_tokens', 'max_completion_tokens']:
            if name in params:
                params['max_output_tokens'] = params.pop(name)
        extra = params.get('extra_body', {})
        reasoning = extra.pop('reasoning', {})
        if 'reasoning_effort' in params:
            effort = params.pop('reasoning_effort')
            if not isinstance(reasoning, dict) or reasoning.get('effort', effort) != effort:
                raise ValueError('推理强度与额外 reasoning 参数冲突，请只保留一致的设置。')
            reasoning = reasoning | {'effort': effort}
        if reasoning:
            params['reasoning'] = reasoning
    if output_limit is not None:
        cap = 'max_output_tokens' if snapshot['protocol'] == 'responses' else 'max_completion_tokens' if 'max_completion_tokens' in params else 'max_tokens'
        for name in ['max_tokens', 'max_completion_tokens', 'max_output_tokens']:
            params.pop(name, None)
        params[cap] = output_limit
    return params


def usage_from(response):
    value = response.model_dump() if hasattr(response, 'model_dump') else response
    usage = value.get('usage') or {}
    incoming = usage.get('prompt_tokens', usage.get('input_tokens'))
    outgoing = usage.get('completion_tokens', usage.get('output_tokens'))
    cache = (usage.get('prompt_tokens_details') or usage.get('input_tokens_details') or {}).get('cached_tokens')
    if cache is None:
        cache = usage.get('prompt_cache_hit_tokens', usage.get('cache_read_input_tokens', 0))
    valid = lambda x: isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) and x >= 0
    if not valid(incoming) or not valid(outgoing) or not valid(cache) or cache > incoming:
        raise ValueError('服务未提供可核对的 token 使用量，调用费用未知；已停止后续请求。')
    return {'input_tokens': incoming, 'output_tokens': outgoing, 'cached_input_tokens': cache}


def priced_usage(response, pricing):
    usage = usage_from(response)
    if not pricing.get('available'):
        raise ValueError('模型价格未知，请先设置当前服务价格；未将未知费用计为零。')
    incoming = pricing['input_per_million']
    outgoing = pricing['output_per_million']
    cached = pricing.get('cached_input_per_million', incoming)
    cost = ((usage['input_tokens'] - usage['cached_input_tokens']) * incoming + usage['cached_input_tokens'] * cached + usage['output_tokens'] * outgoing) / 1000000
    if not math.isfinite(cost) or cost < 0:
        raise ValueError('计价结果无效，已停止后续请求。')
    return {'cost': cost, 'usage': usage, 'pricing_source': pricing['source'], 'cost_known': True}


class ProfilePricing:
    def __init__(self, snapshot, key, model_options=None):
        self.snapshot = copy.deepcopy(snapshot)
        self._key = key
        self.totals = {'input_tokens': 0, 'output_tokens': 0, 'cached_input_tokens': 0, 'model_seconds': 0.0}
        self.last_cost = None
        params = protocol_parameters(snapshot)
        if snapshot['protocol']=='chat' and 'reasoning_effort' in params:
            # Relay aliases are provider-owned; send the explicitly configured parameter for validation there.
            params['allowed_openai_params']=['reasoning_effort']
        super().__init__(**(model_options or {}), model_name=snapshot['model_name'], set_cache_control=None, cost_tracking='default',
                         model_kwargs=params | {'timeout': snapshot['limits']['timeout'], 'num_retries': 0, 'drop_params': False})

    def _query(self, messages, **kwargs):
        return super()._query(messages, **(kwargs | {'api_key': self._key, 'api_base': self.snapshot['api_base']}))

    def _calculate_cost(self, response):
        result = priced_usage(response, self.snapshot['pricing'])
        self.last_cost = result['cost']
        for key, amount in result['usage'].items():
            self.totals[key] += amount
        return result

    def query(self, messages, **kwargs):
        self.last_cost = None
        started = time.monotonic()
        try:
            return super().query(messages, **kwargs)
        finally:
            self.totals['model_seconds'] += time.monotonic() - started

    def serialize(self):
        value = super().serialize()
        value['info']['model_profile'] = {k: self.snapshot[k] for k in ['profile_id', 'profile_name', 'profile_version', 'connection_id', 'model_name', 'protocol', 'configuration_fingerprint', 'pricing']}
        return value


class ProfileChatModel(ProfilePricing, LitellmModel):
    pass


class ProfileResponsesModel(ProfilePricing, LitellmResponseModel):
    pass


def make_profile_model(snapshot, key, model_options=None):
    if not snapshot['pricing'].get('available'):
        raise ValueError('所选模型没有可用计价。请先配置价格，未调用模型。')
    return (ProfileResponsesModel if snapshot['protocol'] == 'responses' else ProfileChatModel)(snapshot, key, model_options)


def tool_roundtrip(snapshot, key, *, max_output_tokens=128, budget_limit=.05):
    """Use a fixed inert tool. Never run provider-returned commands or retry a request."""
    import json
    import httpx
    if type(max_output_tokens) is not int or not 16 <= max_output_tokens <= 256:
        raise ValueError('测试输出上限须在 16–256 token 之间。')
    if not isinstance(budget_limit, (int, float)) or not math.isfinite(budget_limit) or not 0 < budget_limit <= 10:
        raise ValueError('测试费用上限无效。')
    if not snapshot['pricing'].get('available'):
        raise ValueError('测试前请先填写可用价格。')
    # Bound output and allow ample fixed-prompt overhead; actual billing remains provider-owned.
    estimate = (2048 * snapshot['pricing']['input_per_million'] + 2 * max_output_tokens * snapshot['pricing']['output_per_million']) / 1000000
    if estimate > budget_limit:
        raise ValueError('当前价格下的测试估算超过确认上限，请调整上限后重试。')
    result = {'status': 'failed', 'category': 'tool_response', 'profile_id': snapshot['profile_id'], 'fingerprint': snapshot['fingerprint'],
              'calls': 0, 'cost': 0.0, 'usage': {'input_tokens': 0, 'output_tokens': 0, 'cached_input_tokens': 0}, 'checked_at': now(),
              'message': '', 'estimated_limit': budget_limit, 'cost_known': True}
    definition = {'name': 'connection_probe', 'description': 'Return the fixed marker. This tool never executes commands.',
                  'parameters': {'type': 'object', 'properties': {'marker': {'type': 'string', 'enum': ['mini-probe']}}, 'required': ['marker'], 'additionalProperties': False}}
    prompt = 'Call connection_probe with marker mini-probe. After receiving its result, reply OK without another tool call.'
    responses = snapshot['protocol'] == 'responses'
    model_id = snapshot['model_name'].removeprefix('openai/')
    params = protocol_parameters(snapshot, max_output_tokens)
    params = params.pop('extra_body', {}) | params
    if responses:
        body = {'model': model_id, 'input': [{'role': 'user', 'content': prompt}], 'tools': [{'type': 'function', **definition}],
                'tool_choice': {'type': 'function', 'name': 'connection_probe'}, 'max_output_tokens': max_output_tokens, **params}
    else:
        body = {'model': model_id, 'messages': [{'role': 'user', 'content': prompt}], 'tools': [{'type': 'function', 'function': definition}],
                'tool_choice': {'type': 'function', 'function': {'name': 'connection_probe'}},
                ('max_completion_tokens' if 'max_completion_tokens' in snapshot['parameters'] else 'max_tokens'): max_output_tokens, **params}
    endpoint = snapshot['api_base'].rstrip('/') + ('/responses' if responses else '/chat/completions')
    result.update(api_model_id=model_id, protocol=snapshot['protocol'], endpoint=endpoint)
    def request(client, payload):
        result['calls'] += 1
        response = client.post(endpoint, json=payload, headers={'Authorization': 'Bearer ' + key})
        if response.status_code >= 400:
            result['cost_known'] = False
            category = 'upstream' if response.status_code >= 500 else 'parameters' if response.status_code in {400, 422} else 'authentication' if response.status_code in {401, 403} else 'endpoint' if response.status_code == 404 else 'rate_limit' if response.status_code == 429 else 'http'
            result.update(category=category, http_status=response.status_code)
            try:
                error = response.json()
            except ValueError:
                error = {}
            error = error.get('error', error) if isinstance(error, dict) else {}
            error = error if isinstance(error, dict) else {'message': error}
            clean = lambda value: str(redact(str(value).replace(key, '[REDACTED]')))[:600]
            detail = clean(error.get('message') or error.get('detail') or '服务未提供可显示的错误详情。')
            result['provider_code'] = clean(error.get('code') or error.get('type') or '')
            raise ValueError(f'服务返回 HTTP {response.status_code}：{detail} 未自动重试；请核对协议、模型 ID 与参数。')
        value = response.json()
        try:
            priced = priced_usage(value, snapshot['pricing'])
        except ValueError:
            result['cost_known'] = False
            result['category'] = 'pricing_usage'
            raise
        result['cost'] += priced['cost']
        for name, amount in priced['usage'].items():
            result['usage'][name] += amount
        return value
    try:
        with httpx.Client(timeout=min(snapshot['limits']['timeout'], 90), follow_redirects=False, trust_env=False) as client:
            first = request(client, body)
            if responses:
                calls = [item for item in first.get('output', []) if item.get('type') == 'function_call']
                if len(calls) != 1 or calls[0].get('name') != 'connection_probe' or not calls[0].get('call_id') or json.loads(calls[0].get('arguments', '{}')) != {'marker': 'mini-probe'}:
                    raise ValueError('服务没有返回所要求的工具调用，暂不能确认兼容。')
                body['input'] += first['output'] + [{'type': 'function_call_output', 'call_id': calls[0]['call_id'], 'output': '{"ok":true}'}]
            else:
                message = first['choices'][0]['message']
                calls = message.get('tool_calls', [])
                if len(calls) != 1 or calls[0].get('function', {}).get('name') != 'connection_probe' or not calls[0].get('id') or json.loads(calls[0]['function'].get('arguments', '{}')) != {'marker': 'mini-probe'}:
                    raise ValueError('服务没有返回所要求的工具调用，暂不能确认兼容。')
                body['messages'] += [message, {'role': 'tool', 'tool_call_id': calls[0]['id'], 'content': '{"ok":true}'}]
            if result['cost'] >= budget_limit:
                result['category'] = 'budget'
                raise ValueError('已达到测试估算费用上限，未发起第二次请求。单次响应可能超过阈值。')
            body['tool_choice'] = 'none'
            second = request(client, body)
            text = ''.join(part.get('text', '') for item in second.get('output', []) if item.get('type') == 'message' for part in item.get('content', [])) if responses else second['choices'][0]['message'].get('content', '')
            if not text or 'OK' not in text.upper():
                raise ValueError('工具消息已发送，但第二次响应没有给出预期确认。')
            result.update(status='passed', message='工具调用与工具结果回传均通过；未执行任何命令。')
    except (ValueError, KeyError, TypeError, IndexError) as exc:
        result['message'] = str(exc) if isinstance(exc, ValueError) and not isinstance(exc, json.JSONDecodeError) else '响应结构与所选协议不兼容，未自动重试。'
    except httpx.HTTPError as exc:
        result.update(status='incomplete', category='timeout' if isinstance(exc, httpx.TimeoutException) else 'connection', message='请求连接失败或超时，结果未确认，未自动重试；如已发送请求，费用请以服务商记录为准。', cost_known=False)
    return result
