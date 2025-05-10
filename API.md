## API Endpoints

The RNS Status Page provides several API endpoints that can be used externally to fetch status information and configurations.

Public endpoints:

`rnstatus.quad4.io`

### Get All Status Cards

Retrieves the current status of all interfaces. Supports both HTML and JSON output based on the `Accept` header.

- **Endpoint:** `/api/status`
- **Method:** `GET`
- **Returns:**
    - `text/html` (default): An HTML snippet of status cards.
    - `application/json`: A JSON object containing the timestamp, raw status data, and debug information.
- **Example (HTML):**

  ```bash
  curl https://rnstatus.quad4.io/api/status
  ```

- **Example (JSON):**

  ```bash
  curl -H "Accept: application/json" https://rnstatus.quad4.io/api/status
  ```

- **JSON Response Structure:**

  ```json
  {
    "timestamp": "2025-05-10T10:00:00.123456",
    "data": {
      "TCPInterface [quad4/rns.quad4.io:4242]": {
        "name": "quad4/rns.quad4.io:4242",
        "section_type": "TCPInterface",
        "status": "Up",
        "details": {
          "IP Address": "X.X.X.X",
          "Mode": "Point-to-point"
        },
        "first_up_timestamp": 1698397200.0
      }
    },
    "debug": {
      "processing_time_ms": 5.2,
      "cache_hit": true
    }
  }
  ```

### Search Interfaces

Searches for interfaces matching a query string. Supports both HTML and JSON output based on the `Accept` header.

- **Endpoint:** `/api/search`
- **Method:** `GET`
- **Parameters:**
    - `q` (string, required): The search term.
- **Returns:**
    - `text/html` (default): An HTML snippet of matching status cards.
    - `application/json`: A JSON object containing the timestamp, filtered status data, debug information, and the query.
- **Example (HTML):**

  ```bash
  curl "https://rnstatus.quad4.io/api/search?q=TCP"
  ```

- **Example (JSON):**

  ```bash
  curl -H "Accept: application/json" "https://rnstatus.quad4.io/api/search?q=TCP"
  ```

- **JSON Response Structure (similar to /api/status, but `data` is filtered and includes a `query` field):**

  ```json
  {
    "timestamp": "2023-10-27T10:00:05.678910",
    "data": {
      "TCPInterface [quad4/rns.quad4.io:4242]": { /* ... */ }
      // ... other matching interfaces or an empty object if no matches
      // or an "error"/"warning" key if rnstatus failed
    },
    "debug": { /* ... */ },
    "query": "TCP"
  }
  ```

### Export Single Interface Configuration

Exports the configuration for a specific TCP interface as a downloadable text file. The `<interface_name_slug>` in the URL should be the full interface name (e.g., `quad4/rns.quad4.io:4242`) with any `/` characters replaced by `_` (e.g., `quad4_rns.quad4.io:4242`).

- **Endpoint:** `/api/export/<interface_name_slug>`
- **Method:** `GET`
- **Returns:** `text/plain` (with `Content-Disposition: attachment`)
- **Example (for an interface named `mypeer/192.168.1.100:4242`):**

  ```bash
  curl https://rnstatus.quad4.io/api/export/mypeer_192.168.1.100:4242 -o mypeer_config.txt
  ```

### Export All Interface Configurations

Exports configurations for all TCP interfaces as a single downloadable text file.

- **Endpoint:** `/api/export-all`
- **Method:** `GET`
- **Returns:** `text/plain` (with `Content-Disposition: attachment`)
- **Example:**

  ```bash
  curl https://rnstatus.quad4.io/api/export-all -o all_interfaces.txt
  ```

### Real-time Status Events (SSE)

A Server-Sent Events (SSE) stream that pushes updates whenever the status data changes. This is suitable for building real-time monitoring clients.

- **Endpoint:** `/events`
- **Method:** `GET`
- **Returns:** `text/event-stream`
- **Example (using `curl` or a browser JavaScript EventSource):**

  ```bash
  curl -N https://rnstatus.quad4.io/events
  ```

### Debug Information

Provides JSON output with debugging information about the server environment.

- **Endpoint:** `/api/debug`
- **Method:** `GET`
- **Returns:** `application/json`
- **Example:**

  ```bash
  curl https://rnstatus.quad4.io/api/debug
  ```
