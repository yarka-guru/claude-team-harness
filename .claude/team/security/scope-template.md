# Security scope: <T-n> <one-line task>

Owner of the target: <start with "the user" or "employer, local copy">. Active work runs only on a target the user owns or operates, or on a local copy of the employer's code. A live system of the employer, a colleague or a third party does not go here: stop and ask the lead.
Approved by the user: <YYYY-MM-DD, «the words of the user»>
Until the user answers, write "not yet" after "Approved by the user:": then no test starts.

## In scope
- <host, URL, repo, cluster/namespace, cloud account, app - one line each, exact names>

## Permitted actions
- Read-only: <static analysis, reading config, listing, passive requests>
- Active, on a local or throwaway copy only: <exploit runs, fuzzing, scans, prompt-injection tests>
- State-changing on a live target: none by an agent. Commands go to the lead, the user runs them.

## Network boundary
- Do not contact hosts outside: <list / CIDR / "localhost and the stand named above">
- Do not use the internet during the run except: <nothing | package index before the start>

## Out of scope
- Production data of real users, other tenants, neighbour containers and nodes, cloud metadata endpoints, third-party services, anything not listed above.
- <task-specific exclusions>

## Stand
- Where it runs: <local install | local container or Kubernetes stand | isolated VM without egress | dev replica>
- Tools installed before the start: <list>
- What looks impossible or needs closer watching: <none | ...>

## Stop conditions
- Anything outside this file was reached, an isolation boundary gave way, real user data or foreign credentials showed up, or a control named here is absent: stop, `blocker` to the lead, keep the evidence.
