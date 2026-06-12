export interface Agent {
  name: string;
  description: string;
  model: string | null;
  tool_groups: string[] | null;
  skills: string[] | null;
  soul?: string | null;
  /**
   * True if this agent comes from the system-default layer
   * (`users/default/agents/`). Visible to every authenticated user;
   * writable only by admins (target=default).
   */
  is_default?: boolean;
}

export interface CreateAgentRequest {
  name: string;
  description?: string;
  model?: string | null;
  tool_groups?: string[] | null;
  skills?: string[] | null;
  soul?: string;
  /**
   * Which on-disk layer to write into. "user" (default) targets the
   * caller's per-user dir; "default" targets the system-default dir and
   * requires admin privileges on the backend.
   */
  target?: "user" | "default";
}

export interface UpdateAgentRequest {
  description?: string | null;
  model?: string | null;
  tool_groups?: string[] | null;
  skills?: string[] | null;
  soul?: string | null;
  /**
   * Which on-disk layer to write into. "user" (default) targets the
   * caller's per-user dir; "default" targets the system-default dir and
   * requires admin privileges on the backend.
   */
  target?: "user" | "default";
}
