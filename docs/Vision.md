# TotalOrch — Vision

## Vision

The AI ecosystem already provides mature solutions for model execution, provider abstraction and routing. This project intentionally builds **above** those systems rather than replacing them.

The TotalOrch is an intelligent orchestration layer that coordinates AI agents, dynamically builds workflows, learns from previous executions, and understands project context while remaining independent from any execution gateway contract. The default distribution includes OmniRoute as a managed internal execution gateway, while the Orchestrator keeps ownership of model intelligence, agent pools, and semantic routing.

## Core Philosophy

- The Orchestrator decides **what** should happen.
- The Gateway decides **how** models are executed.
- Workflows are **dynamic**, not hard-coded.

## Dynamic Workflow Generation

Unlike traditional multi-agent systems with fixed pipelines, this platform generates a workflow for each request.

Workflow composition may depend on:

- Task type and complexity
- Project context
- User policies
- Available models
- Cost and latency constraints
- Previous execution history
- Learning engine recommendations

Example outcomes:

### Simple Bug
User → Bug Analyzer → Fix Agent → Reviewer → Done

### Small Feature
User → Coder → Reviewer → Done

### New Feature
User → Planner → Architect → Task Decomposer → Multiple Coders → Reviewer → Tester → Documentation → Done

Users may also provide reusable workflow templates or completely custom workflows.

## Design Principles

- Gateway-Independent Contract
- Provider Agnostic
- IDE Agnostic
- Agent-Oriented Architecture
- Workflow First
- Project Knowledge
- Continuous Learning
- Policy Driven
- Intelligent Defaults
- Explainable Routing

## Responsibilities

### The Orchestrator owns

- Planning
- Dynamic workflow generation
- Agent lifecycle
- Multi-agent collaboration
- Knowledge management
- Project memory
- Learning engine
- Decision engine
- Policy enforcement
- Context management
- Model registry and capability normalization
- Default agent profiles and automatically generated model pools
- Role-aware model selection and explainable routing

### The Gateway owns

- Provider abstraction
- Provider adapter registry and provider connections
- Model execution
- Endpoint-level routing and connection selection
- Retry / Fallback
- Streaming
- Health checks
- Telemetry
- Authentication

### Default Gateway Implementation

OmniRoute is the default managed gateway implementation. It runs as an internal service or sidecar and is not exposed as a configuration prerequisite for end users.

The Orchestrator communicates with OmniRoute through an adapter and API contract. Users configure providers through the Orchestrator UI; provider credentials, OAuth flows, connection testing, and low-level execution remain owned by OmniRoute.

The Orchestrator does not duplicate OmniRoute's provider adapters. Instead, it synchronizes OmniRoute's available providers, connections, models, capabilities, health, latency, cost, and quota data into its own normalized Model Intelligence Layer.

### Intelligent Defaults

The first-run experience must be usable without manually defining agents or pools. After provider discovery and catalog synchronization, the platform builds default profiles for Planner, Developer/Coder, Reviewer, Researcher, and other core agents.

Default pools combine:

- Model capabilities and context limits
- models.dev and OpenRouter metadata
- Provider and endpoint health
- Real-time latency, quota, and availability
- Task-specific execution outcomes
- User preferences and policy constraints

Users may customize or override these defaults, but manual model-pool construction is an advanced option rather than a prerequisite.

## Long-Term Vision

Create an orchestration platform capable of assembling the right team of AI agents for every task, automatically generating the optimal workflow, continuously improving through learning, and remaining portable across any execution gateway.
