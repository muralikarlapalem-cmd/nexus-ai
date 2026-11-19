import client from './client'

export const ticketApi = {
  list: (params) => {
  const cleaned = Object.fromEntries(
    Object.entries(params || {}).filter(([, v]) => v !== '' && v != null)
  )
  return client.get('/tickets/', { params: cleaned })
},
  get:          (id)     => client.get(`/tickets/${id}`),
  create:       (data)   => client.post('/tickets/', data),
  updateStatus: (id, data) => client.post(`/tickets/${id}/status`, data),
  update:       (id, data) => client.patch(`/tickets/${id}`, data),
}

export const ragApi = {
  query:        (data)   => client.post('/rag/query', data),
  search:       (data)   => client.post('/rag/search', data),
  stats:        ()       => client.get('/rag/stats'),
  ingestText:   (data)   => client.post('/rag/ingest/text', data),
  ingestTickets: ()      => client.post('/rag/ingest/tickets'),
}

export const agentApi = {
  run:    (id)        => client.post(`/agents/run/${id}`),
  status: (id)        => client.get(`/agents/status/${id}`),
  trace:  (id)        => client.get(`/agents/trace/${id}`),
  resume: (id, data)  => client.post(`/agents/resume/${id}`, data),
  retry:  (id)        => client.post(`/agents/retry/${id}`),
  info:   ()          => client.get('/agents/info'),
  active: (params)    => client.get('/agents/active', { params }),
  langsmith: (id)     => client.get(`/agents/langsmith/${id}`),
}

export const analyticsApi = {
  summary: (params)    => client.get('/analytics/summary', { params }),
}

export const integrationsApi = {
  types:        ()              => client.get('/integrations/types'),
  list:         ()              => client.get('/integrations/'),
  get:          (id)            => client.get(`/integrations/${id}`),
  create:       (data)          => client.post('/integrations/', data),
  update:       (id, data)      => client.patch(`/integrations/${id}`, data),
  remove:       (id)            => client.delete(`/integrations/${id}`),
  test:         (id)            => client.post(`/integrations/${id}/test`),
  testSettings: (data)          => client.post('/integrations/test', data),
  sync:         (id, params)    => client.post(`/integrations/${id}/sync`, null, { params }),
  runs:         (id, params)    => client.get(`/integrations/${id}/runs`, { params }),
  writeBack:    (ticketId)      => client.post(`/integrations/tickets/${ticketId}/write-back`),
}

export const authApi = {
  login:    (data) => client.post('/auth/login', data),
  me:       ()     => client.get('/auth/me'),
  register: (data) => client.post('/auth/register', data),
}
