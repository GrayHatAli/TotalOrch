# Initial MVP Task Type

The first end-to-end workflow targets a software-development task with two
allowed shapes:

- **Feature:** implement a bounded change described by the user and validate
  it with available tests.
- **Bug fix:** reproduce or reason about a reported defect, implement the
  smallest safe correction, and validate the result with available tests.

Both shapes share the same initial workflow contract:

```text
Planner → Developer/Coder → Reviewer → optional test execution
```

The task must identify a workspace/repository, contain a natural-language
request, and produce a structured result with changed files, validation
evidence, and unresolved risks. General research, production deployment, and
unbounded multi-agent collaboration are outside this first task boundary.
