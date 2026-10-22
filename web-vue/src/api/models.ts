import apiClient from './client'

export interface OpenAIModel {
  id: string
  object?: string
  created?: number
  owned_by?: string
  root?: string
  parent?: string | null
}

export interface ModelListResponse {
  object?: string
  data: OpenAIModel[]
}

export interface ModelCatalogResponse {
  object?: 'model_catalog' | string
  chat_models: string[]
  image_models: string[]
  image_edit_models?: string[]
  video_models?: string[]
  all_models?: string[]
  schema_version?: number
  generated_at?: string
  revision?: string
  defaults?: {
    chat_model?: string
    image_model?: string
  }
  capabilities?: {
    image_upscale?: boolean
    // 支持 2K/4K 高分辨率尺寸的图片模型；Studio 的尺寸预设据此过滤。
    high_resolution_image_models?: string[]
  }
  source?: {
    chat?: string
    image?: string
    grok?: string
  }
  openai_models_endpoint?: string
}

export const modelsApi = {
  catalog: () => apiClient.get<never, ModelCatalogResponse>('/api/model-catalog'),
  list: () => apiClient.get<never, ModelListResponse>('/v1/models'),
}
