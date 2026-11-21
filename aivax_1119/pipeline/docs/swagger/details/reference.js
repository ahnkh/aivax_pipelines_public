/* 필드 표와 예제는 Swagger 원본에서 읽어 상세 명세와 일치시킨다. */
(() => {
  const element = (tag, text) => {
    const node = document.createElement(tag);
    if (text !== undefined) node.textContent = text;
    return node;
  };
  const resolve = (schema, spec) => schema?.$ref
    ? spec.components.schemas[schema.$ref.split('/').pop()]
    : schema;
  const displayType = (schema, spec) => {
    const value = resolve(schema, spec);
    if (value.anyOf) return value.anyOf.map(item => displayType(item, spec)).join(' | ');
    if (value.type === 'array') return `array<${displayType(value.items, spec)}>`;
    if (Array.isArray(value.type)) return value.type.join(' | ');
    return value.type || 'any';
  };
  const allowsNull = (schema, spec) => {
    const value = resolve(schema, spec);
    if (!value.type && !value.anyOf) {
      if (Object.hasOwn(value, 'const')) return value.const === null;
      return value.enum ? value.enum.includes(null) : true;
    }
    return value.type === 'null' || (Array.isArray(value.type) && value.type.includes('null'))
      || Boolean(value.anyOf?.some(item => allowsNull(item, spec)));
  };
  const json = value => JSON.stringify(value, null, 2);
  const codeBlock = value => {
    const pre = element('pre');
    pre.append(element('code', json(value)));
    return pre;
  };
  const fieldTable = (schema, spec) => {
    const value = resolve(schema, spec);
    const wrapper = element('div');
    wrapper.className = 'table-scroll';
    const table = element('table');
    const head = element('thead');
    const heading = element('tr');
    for (const label of ['필드', 'JSON 타입', '필수', 'null', '기본값', '설명']) {
      heading.append(element('th', label));
    }
    head.append(heading);
    table.append(head);
    const body = element('tbody');
    for (const [name, field] of Object.entries(value.properties || {})) {
      const row = element('tr');
      const type = displayType(field, spec);
      let description = field.description || resolve(field, spec).description || '';
      if (field.enum) description += `\n허용값: ${JSON.stringify(field.enum)}`;
      if (Object.hasOwn(field, 'const')) description += `\n고정값: ${JSON.stringify(field.const)}`;
      if (field.examples) description += `\n예: ${JSON.stringify(field.examples)}`;
      const cells = [name, type, value.required?.includes(name) ? '예' : '생략 가능',
        allowsNull(field, spec) ? '허용' : '불가',
        Object.hasOwn(field, 'default') ? JSON.stringify(field.default) : '—', description];
      for (const text of cells) row.append(element('td', text));
      body.append(row);
    }
    table.append(body);
    wrapper.append(table);
    return wrapper;
  };
  const nestedSchemas = (schema, spec, target) => {
    const visited = new Set();
    if (schema.$ref) visited.add(schema.$ref);
    const visit = (value, path) => {
      if (!value || typeof value !== 'object') return;
      if (value.$ref) {
        if (visited.has(value.$ref)) return;
        visited.add(value.$ref);
        const resolved = resolve(value, spec);
        if (resolved.properties) {
          const details = element('details');
          details.append(element('summary', `${path} — ${value.$ref.split('/').pop()}`));
          details.append(fieldTable(resolved, spec));
          target.append(details);
        }
        visit(resolved, path);
        return;
      }
      for (const [name, field] of Object.entries(value.properties || {})) {
        visit(field, path ? `${path}.${name}` : name);
      }
      if (value.items) visit(value.items, `${path}[]`);
      if (typeof value.additionalProperties === 'object') visit(value.additionalProperties, `${path}.*`);
      for (const variant of value.anyOf || []) visit(variant, path);
    };
    visit(resolve(schema, spec), '');
  };
  const examples = (media, target, label) => {
    target.append(element('h3', label));
    const values = media.examples || (Object.hasOwn(media, 'example')
      ? { default: { summary: label, value: media.example } } : {});
    for (const [name, sample] of Object.entries(values)) {
      const details = element('details');
      details.append(element('summary', sample.summary || name));
      details.append(codeBlock(sample.value));
      target.append(details);
    }
  };
  const load = async () => {
    const response = await fetch('../openapi.json');
    if (!response.ok) throw new Error(`명세 조회 실패: HTTP ${response.status}`);
    const spec = await response.json();
    const operation = spec.paths[document.body.dataset.apiPath].post;
    const request = operation.requestBody.content['application/json'];
    const success = operation.responses['200'].content['application/json'];
    document.querySelector('[data-request-fields]').append(fieldTable(request.schema, spec));
    nestedSchemas(request.schema, spec, document.querySelector('[data-request-details]'));
    document.querySelector('[data-response-fields]').append(fieldTable(success.schema, spec));
    nestedSchemas(success.schema, spec, document.querySelector('[data-response-details]'));
    examples(request, document.querySelector('[data-request-examples]'), '요청 예제');
    examples(success, document.querySelector('[data-response-examples]'), '성공 응답 예제');
    const errors = document.querySelector('[data-errors]');
    for (const [status, definition] of Object.entries(operation.responses)) {
      if (Number(status) < 400) continue;
      const details = element('details');
      details.append(element('summary', `HTTP ${status}`));
      details.append(element('p', definition.description));
      const media = definition.content?.['application/json'];
      if (media?.schema) {
        details.append(fieldTable(media.schema, spec));
        nestedSchemas(media.schema, spec, details);
      }
      if (media?.example) details.append(codeBlock(media.example));
      errors.append(details);
    }
    document.querySelector('[data-load-status]').textContent = '';
    document.body.dataset.loaded = 'true';
  };
  load().catch(error => {
    const status = document.querySelector('[data-load-status]');
    status.className = 'error';
    status.textContent = `${error.message}. HTTP 서버에서 페이지를 열어 주세요.`;
  });
})();
