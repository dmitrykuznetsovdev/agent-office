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
  // an epic role: what its Claude Code job file says (working, idle, done, stopped)
  job?: string
}

export type LogLine = { at: number; who: string; text: string }

export type Producer = { isBusy: boolean; activity: string; at: number; calls: number; cost: number; model: string; tool: string; waitingFor?: string; owner?: boolean }

export type Studio = {
  agents: Agent[]
  producer: Producer
  cost: number
  log: LogLine[]
  since: number
  office: Office
}

export type OfficeDesk = { type: string; name: string; short?: string; prop: string; shirt: string; hair: string; skin: number; locked?: boolean }

export type Office = {
  folder: string
  title: string
  // note: the plaque's second line instead of the head count;
  // track: an epic floor's stages left to right, each [label, passed | next | rejected | ahead]
  zones: Array<{ id?: string; name: string; desks: OfficeDesk[]; note?: string; track?: Array<[string, string]> | null }>
  guests: Record<string, { name: string; prop: string }>
  saved?: boolean
  lang?: string
  hidden?: number
  generating?: boolean
  provider?: string
  accent?: string
  // an orchestrating-epics epic: the coordinator's context against its compaction line, packages landed and waiting
  epic?: { context: number | null; limit: number | null; done?: string[]; queue?: string[] }
}
