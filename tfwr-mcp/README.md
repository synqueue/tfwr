# TFWR MCP Server

MCP server for [The Farmer Was Replaced](https://store.steampowered.com/app/2060160/)

## Setup

### 1. Install dependencies
```
pip install "mcp[cli]"
```

### 2. Add MCP


```json
{
  "mcpServers": {
    "tfwr": {
      "command": "python",
      "args": ["C:/Users/synqueue/Desktop/tfwr-mcp/server.py"]
    }
  }
}
```

### 3. Enable File Watcher

1. Open the game
2. Go to **Options**
3. Enable **File Watcher** (this disables Autosave automatically)

## Environment variable

Set `TFWR_DATA_PATH` to override the auto-detected game data path:
```
set TFWR_DATA_PATH=D:\custom\path\to\TheFarmerWasReplaced
```
