# Sprint 7: TUI/Dashboard and Advanced Observability

Building on Sprint 6's streaming infrastructure, Sprint 7 will introduce rich interactive dashboards and advanced workflow features.

## Phase 1: TUI Foundation (Textual Framework)

### Goals
- Create interactive terminal UI for monitoring orchestration runs
- Provide real-time visibility into multiple concurrent runs
- Enable user interaction (pause, resume, inspect) from the dashboard

### Implementation Plan

**1. TUI Framework Selection**
- Use [Textual](https://textual.textualize.io/) for building the dashboard
- Rich integration (already using Rich for CLI output)
- Widget-based architecture for composable UI

**2. Core Dashboard Widgets**

```python
# Main dashboard layout
class DuetDashboard(App):
    """Main TUI application for Duet monitoring."""

    def compose(self) -> ComposeResult:
        yield Header()
        yield RunList()          # Active/recent runs
        yield EventStream()      # Real-time event feed
        yield StatsPanel()       # Aggregate statistics
        yield Footer()
```

**Widgets to Implement:**
- `RunList`: Scrollable list of runs with status, phase, iteration
- `EventStream`: Live scrolling feed of streaming events (last 50 events)
- `StatsPanel`: Token usage, success rate, average durations
- `DetailView`: Drill-down into specific run with iteration timeline
- `LogViewer`: Full event transcript with search/filter

**3. Data Binding**

```python
# Poll SQLite database for updates
class DashboardController:
    def __init__(self, db: DuetDatabase):
        self.db = db
        self.watch_runs()  # Background thread polling for changes

    async def watch_runs(self):
        """Poll database every 2 seconds for run updates."""
        while True:
            runs = self.db.list_runs(limit=20)
            # Update widgets with new data
            await asyncio.sleep(2)
```

**4. Keyboard Shortcuts**
- `r` - Refresh data
- `Enter` - View run details
- `e` - Show events for selected run
- `q` - Quit dashboard
- `/` - Search/filter
- `?` - Help/keybindings

### Technical Approach

**Database Polling:**
- Background task polls `duet.db` every 2 seconds
- Detects new runs, phase changes, iteration progress
- Pushes updates to TUI widgets via reactive data binding

**Event Streaming Integration:**
- During active runs, hook directly into orchestrator event stream
- Real-time event feed without polling
- Highlight active run in RunList

**Layout:**
```
┌─ Duet Dashboard ────────────────────────────────────────────┐
│                                                              │
│ Active Runs (3)                    ┌─ Event Stream ────────┐│
│ ┌────────────────────────────────┐ │ 14:23:05 ✓ Turn done ││
│ │ ▶ run-abc123  PLAN   iter 2/5 │ │ 14:23:03 … Reasoning ││
│ │   run-def456  DONE   iter 3/5 │ │ 14:23:01 ✓ Message   ││
│ │   run-ghi789  REVIEW iter 4/5 │ │ 14:22:58 Thread start││
│ └────────────────────────────────┘ │ 14:22:55 ✓ Item done ││
│                                     └───────────────────────┘│
│ Stats                                                        │
│  Total Runs: 127 | Success: 89% | Avg Duration: 8.5m       │
│                                                              │
│ [r]efresh [e]vents [q]uit [?]help                          │
└──────────────────────────────────────────────────────────────┘
```

---

## Phase 2: Advanced Workflow Features

### Resume/Pause Functionality

**Goal**: Allow users to pause and resume orchestration runs

**Implementation:**
```python
# Add pause/resume to RunSnapshot
class RunSnapshot:
    ...
    paused: bool = False
    paused_at: Optional[datetime] = None

# Orchestrator checks pause flag
def run(self, run_id: Optional[str] = None) -> RunSnapshot:
    while current_phase != Phase.DONE:
        # Check for pause signal
        if self._check_pause_requested(snapshot.run_id):
            snapshot.paused = True
            snapshot.paused_at = datetime.now()
            self.artifacts.checkpoint(snapshot)
            break

        # Continue with iteration...
```

**TUI Integration:**
- Keyboard shortcut `p` to pause selected run
- Resume button in DetailView
- Visual indicator for paused runs

### Retry/Backoff Policies

**Goal**: Automatically retry failed adapter calls with exponential backoff

**Configuration:**
```yaml
workflow:
  retry:
    enabled: true
    max_attempts: 3
    backoff_multiplier: 2.0  # 1s → 2s → 4s
    retry_on_errors:
      - "timeout"
      - "connection"
      - "rate_limit"
```

**Implementation:**
```python
def _call_adapter_with_retry(self, adapter, request, max_attempts=3):
    """Call adapter with exponential backoff on failures."""
    for attempt in range(1, max_attempts + 1):
        try:
            return adapter.stream(request, on_event=...)
        except (TimeoutError, ConnectionError) as e:
            if attempt < max_attempts:
                wait_seconds = 2 ** (attempt - 1)
                self.console.log(f"Retry {attempt}/{max_attempts} after {wait_seconds}s")
                time.sleep(wait_seconds)
            else:
                raise
```

### Health Metrics

**Goal**: Track adapter health and performance over time

**New SQLite Table:**
```sql
CREATE TABLE adapter_metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    iteration INTEGER,
    adapter_name TEXT NOT NULL,
    duration_seconds REAL,
    success BOOLEAN,
    error_type TEXT,
    timestamp TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES runs(run_id)
);
```

**Metrics Collected:**
- Adapter call duration
- Success/failure rate per adapter
- Error types and frequencies
- Token efficiency (tokens per second)
- Cache hit rates

**Dashboard Display:**
- Adapter health panel showing success rates
- Performance trends (response times over time)
- Error breakdown by type
- Recommendations (e.g., "Codex timeout rate: 15% - consider increasing timeout")

---

## Phase 3: Advanced TUI Features

### Multi-Run Monitoring

**Goal**: Monitor multiple active runs simultaneously

**Features:**
- Split-pane view showing up to 4 concurrent runs
- Each pane shows live events for that run
- Automatic layout based on active run count
- Focus switching with Tab/Arrow keys

### Search and Filtering

**Goal**: Powerful search across all historical data

**Search Modes:**
- Full-text search across prompts, responses, notes
- Event payload search (e.g., find all parse_errors)
- Time-range filtering with calendar widget
- Regular expression support for advanced queries

**Implementation:**
```python
# Add full-text search to SQLite
CREATE VIRTUAL TABLE events_fts USING fts5(
    event_type, payload, timestamp
);

# Search query
SELECT * FROM events_fts
WHERE payload MATCH 'timeout OR error'
ORDER BY timestamp DESC;
```

### Notification System

**Goal**: Alert users to important events

**Notification Types:**
- Run completion (DONE/BLOCKED)
- Guardrail breach (max iterations, timeout)
- Human approval required
- Parse errors or adapter failures

**Delivery Methods:**
- In-TUI notifications (toast/banner)
- System notifications (desktop alerts)
- Webhook calls (Slack, Discord, custom URLs)
- Optional: Email via SMTP

**Configuration:**
```yaml
notifications:
  enabled: true
  channels:
    - type: "system"  # Desktop notifications
    - type: "webhook"
      url: "https://hooks.slack.com/..."
      events: ["run.completed", "run.blocked"]
```

---

## Phase 4: Web Dashboard (Optional/Future)

### Goals
- Remote monitoring via web browser
- Multi-user access to shared Duet instances
- Richer visualizations (charts, graphs, timelines)

### Technical Stack
- FastAPI backend serving SQLite data
- WebSocket for real-time event streaming
- React/Vue frontend for rich visualizations
- Authentication for multi-user deployments

### Features
- Timeline visualization of run history
- Interactive event explorer with filtering
- Diff viewer for git changes per iteration
- Export reports (PDF, CSV, JSON)
- Shareable run permalinks

---

## Phase 5: Integration and Ecosystem

### CI/CD Integration

**Goal**: Run Duet in automated pipelines

**Features:**
- Exit codes for success/failure
- JSON output mode for all commands
- Headless mode (no TTY required)
- Artifact upload to cloud storage

**Example GitHub Actions:**
```yaml
- name: Run Duet Orchestration
  run: |
    duet run --quiet --format json > run-result.json
    if [ $? -ne 0 ]; then
      echo "Orchestration failed"
      cat run-result.json | jq '.notes'
      exit 1
    fi
```

### External Tool Integration

**Planned Integrations:**
- **Jira/Linear**: Create tickets from blocked runs
- **GitHub**: Auto-create PRs from successful runs
- **Datadog/NewRelic**: Export metrics for monitoring
- **S3/GCS**: Archive artifacts and logs

---

## Implementation Priority

### Must-Have (Sprint 7 Core)
1. Basic Textual TUI with RunList + EventStream
2. Resume/pause functionality
3. Retry policies with exponential backoff
4. Health metrics collection

### Nice-to-Have (Sprint 7 Stretch)
5. Multi-run monitoring (split-pane)
6. Advanced search/filtering
7. Notification system (in-TUI + desktop)

### Future (Post-Sprint 7)
8. Web dashboard with FastAPI + React
9. CI/CD integration templates
10. External tool integrations (Jira, GitHub, monitoring)

---

## Technical Considerations

### Performance
- SQLite handles thousands of events efficiently with indexes
- Polling at 2-second intervals is lightweight
- Event table can be pruned periodically (e.g., keep last 30 days)

### Dependencies
- **Textual**: ~500KB, pure Python, actively maintained
- **FastAPI**: Only needed for web dashboard (optional)
- **Watchdog**: For filesystem event watching (alternative to polling)

### Backward Compatibility
- TUI is opt-in (new `duet dashboard` command)
- Existing CLI commands remain unchanged
- No breaking changes to config or artifacts

---

## Success Criteria

Sprint 7 is complete when:
- ✅ Basic TUI dashboard displays active runs + live events
- ✅ Users can pause/resume runs from dashboard
- ✅ Retry policies handle transient failures
- ✅ Health metrics tracked and displayed
- ✅ Documentation covers TUI usage and keybindings
- ✅ Tests cover TUI widgets and workflow features

---

## Estimated Effort

| Phase | Complexity | Time Estimate |
|-------|-----------|---------------|
| TUI Foundation | Medium | 20-30 hours |
| Resume/Pause | Medium | 12-18 hours |
| Retry Policies | Low | 8-12 hours |
| Health Metrics | Low | 8-12 hours |
| Advanced TUI | High | 30-40 hours |
| **Total Sprint 7** | | **78-112 hours** |

Web dashboard and external integrations are post-Sprint 7 (separate epic).

---

## Next Steps

1. Review and refine Sprint 7 plan with stakeholders
2. Break down TUI foundation into specific tasks
3. Set up Textual development environment
4. Create TUI widget prototypes
5. Begin implementation with RunList widget

**Sprint 7 builds directly on Sprint 6's streaming infrastructure - the event persistence and real-time data flow enable all TUI features.**
