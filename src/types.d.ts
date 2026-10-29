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
}

export type LogLine = { at: number; who: string; text: string }

export type Studio = {
  agents: Agent[]
  producer: { isBusy: boolean; activity: string; at: number; calls: number; cost: number }
  cost: number
  log: LogLine[]
  since: number
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
