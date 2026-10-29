export type Agent = {
  id: string
  type: string
  desc: string
  isWorking: boolean
  activity: string
  startedAt: number
  endedAt: number
  calls: number
  cost: number
  model: string
  tool: string
  at: number
  finished: boolean
}

export type LogLine = { at: number; who: string; text: string }

export type Producer = { isBusy: boolean; activity: string; at: number; calls: number; cost: number; model: string; tool: string }

export type Studio = {
  agents: Agent[]
  producer: Producer
  cost: number
  log: LogLine[]
  since: number
  office: Office
}

export type OfficeDesk = { type: string; name: string; prop: string; shirt: string; hair: string; skin: number }

export type Office = {
  folder: string
  title: string
  zones: Array<{ name: string; desks: OfficeDesk[] }>
  guests: Record<string, { name: string; prop: string }>
  saved?: boolean
  generating?: boolean
}
