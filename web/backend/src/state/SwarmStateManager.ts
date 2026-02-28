export interface SwarmRecord {
  swarm_id: string;
  alias: string;
  job_id: string;
  node_count: number;
  status: string;
  slurm_state?: string;
  created_at: number;
}

export class SwarmStateManager {
  private swarms = new Map<string, SwarmRecord>();
  private aliasIndex = new Map<string, string>();

  createSwarm(swarm_id: string, alias: string, job_id: string, node_count: number) {
    const normalized = alias.toLowerCase();
    if (this.aliasIndex.has(normalized)) {
      throw new Error('Alias already exists');
    }

    const record: SwarmRecord = {
      swarm_id,
      alias,
      job_id,
      node_count,
      status: 'running',
      created_at: Date.now()
    };

    this.swarms.set(swarm_id, record);
    this.aliasIndex.set(normalized, swarm_id);
    return record;
  }

  getByAlias(alias: string) {
    const swarm_id = this.aliasIndex.get(alias.toLowerCase());
    if (!swarm_id) return undefined;
    return this.swarms.get(swarm_id);
  }

  getById(id: string) {
    return this.swarms.get(id);
  }

  list() {
    return Array.from(this.swarms.values());
  }

  remove(swarm_id: string) {
    const record = this.swarms.get(swarm_id);
    if (!record) return;
    this.aliasIndex.delete(record.alias.toLowerCase());
    this.swarms.delete(swarm_id);
  }

  updateStatus(swarm_id: string, status: string, slurm_state?: string) {
    const swarm = this.swarms.get(swarm_id);
    if (!swarm) return;
    swarm.status = status;
    swarm.slurm_state = slurm_state;
  }
}
