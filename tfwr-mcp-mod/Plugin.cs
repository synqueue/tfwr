using System;
using System.Collections.Concurrent;
using System.Globalization;
using System.IO;
using System.Net;
using System.Reflection;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading;
using BepInEx;
using HarmonyLib;
using UnityEngine;

namespace TFWRMCPBridge
{
    [BepInPlugin("com.iris.tfwr.mcpbridge", "TFWR MCP Bridge", "1.0.0")]
    public class Plugin : BaseUnityPlugin
    {
        internal static BepInEx.Logging.ManualLogSource Log;

        private const string PREFIX = "http://localhost:7070/";
        private HttpListener _listener;

        // Work items that need to run on Unity's main thread
        private readonly ConcurrentQueue<MainThreadWork> _mainQueue = new ConcurrentQueue<MainThreadWork>();

        // Cached reflection fields for accessing private MainSim internals
        private static FieldInfo _simField;
        private static FieldInfo _lockField;

        // ── Lifecycle ────────────────────────────────────────────────────────

        private void Awake()
        {
            Log = base.Logger;

            new Harmony("com.iris.tfwr.mcpbridge").PatchAll();

            _listener = new HttpListener();
            _listener.Prefixes.Add(PREFIX);
            _listener.Start();

            var thread = new Thread(ListenerLoop) { IsBackground = true, Name = "MCPBridge" };
            thread.Start();

            Log.LogInfo($"TFWR MCP Bridge listening on {PREFIX}");
        }

        private void Update()
        {
            while (_mainQueue.TryDequeue(out var work))
            {
                try   { work.Result = work.Handler(); }
                catch (Exception ex) { work.Result = JsonError(ex.Message); }
                work.Done.Set();
            }
        }

        private void OnDestroy()
        {
            _listener?.Stop();
        }

        // ── HTTP listener (background thread) ────────────────────────────────

        private void ListenerLoop()
        {
            while (_listener.IsListening)
            {
                try
                {
                    var ctx = _listener.GetContext();
                    ThreadPool.QueueUserWorkItem(_ => HandleContext(ctx));
                }
                catch (HttpListenerException) { break; }
                catch (Exception ex) { Log.LogError(ex); }
            }
        }

        private void HandleContext(HttpListenerContext ctx)
        {
            var req  = ctx.Request;
            var resp = ctx.Response;
            string body = "";
            if (req.HasEntityBody)
                using (var sr = new StreamReader(req.InputStream, req.ContentEncoding))
                    body = sr.ReadToEnd();

            string json;
            try   { json = Route(req.HttpMethod, req.Url.AbsolutePath.TrimEnd('/'), body, req.Url.Query ?? ""); }
            catch (Exception ex) { json = JsonError(ex.Message); }

            byte[] bytes = Encoding.UTF8.GetBytes(json);
            resp.ContentType       = "application/json";
            resp.ContentLength64   = bytes.Length;
            resp.Headers["Access-Control-Allow-Origin"] = "*";
            resp.OutputStream.Write(bytes, 0, bytes.Length);
            resp.Close();
        }

        private string Route(string method, string path, string body, string query = "")
        {
            if (method == "GET"    && path == "/api/status")      return OnMainThread(Status);
            if (method == "GET"    && path == "/api/output")      return OnMainThread(Output);
            if (method == "DELETE" && path == "/api/output")      return OnMainThread(ClearOutput);
            if (method == "GET"    && path == "/api/state")       return OnMainThread(State);
            if (method == "GET"    && path == "/api/scripts")     return OnMainThread(Scripts);
            if (method == "POST"   && path == "/api/run")         return OnMainThread(() => Run(body));
            if (method == "POST"   && path == "/api/stop")        return OnMainThread(Stop);
            if (method == "POST"   && path == "/api/step")        return OnMainThread(Step);
            if (method == "GET"    && path == "/api/grid")        return OnMainThread(Grid);
            if (method == "GET"    && path == "/api/shop")        return OnMainThread(Shop);
            if (method == "POST"   && path == "/api/buy")         return OnMainThread(() => Buy(body));
            if (method == "GET"    && path == "/api/docs")        return OnMainThread(GetDocs);
            if (method == "POST"   && path == "/api/docs/close")  return OnMainThread(() => CloseDocsWindow(body));
            if (method == "GET"    && path == "/api/docs/fetch")  return OnMainThread(() => FetchDoc(query));
            return JsonError($"Unknown route: {method} {path}");
        }

        // Dispatch work to Unity main thread and wait for the result
        private string OnMainThread(Func<string> handler)
        {
            var work = new MainThreadWork { Handler = handler };
            _mainQueue.Enqueue(work);
            if (!work.Done.Wait(TimeSpan.FromSeconds(5)))
                return JsonError("Main-thread timeout after 5 s");
            return work.Result;
        }

        // ── Reflection helpers ───────────────────────────────────────────────

        /// <summary>
        /// Gets the private Simulation object from MainSim under lockSimulation.
        /// Returns null if reflection fails or MainSim is not ready.
        /// The caller must already hold lockObj before calling into farm/grid.
        /// </summary>
        private static bool TryGetSimAndLock(out Simulation sim, out object lockObj)
        {
            sim = null;
            lockObj = null;

            var mainSim = MainSim.Inst;
            if (mainSim == null) return false;

            if (_simField == null)
            {
                _simField  = typeof(MainSim).GetField("sim",            BindingFlags.NonPublic | BindingFlags.Instance);
                _lockField = typeof(MainSim).GetField("lockSimulation", BindingFlags.NonPublic | BindingFlags.Instance);
            }
            if (_simField == null || _lockField == null) return false;

            lockObj = _lockField.GetValue(mainSim);
            sim     = (Simulation)_simField.GetValue(mainSim);
            return sim != null && lockObj != null;
        }

        // ── Route handlers (run on main thread via Update) ───────────────────

        private string Status()
        {
            var sim = MainSim.Inst;
            if (sim == null) return JsonError("MainSim not ready");
            var time   = sim.GetCurrentTime();
            var wsz    = sim.GetWorldSize();
            return
                $"{{\"isExecuting\":{B(sim.IsExecuting())}," +
                $"\"isSimulating\":{B(sim.IsSimulating())}," +
                $"\"worldSize\":{wsz.x}," +
                $"\"timeSec\":{time.Seconds.ToString("F3", CultureInfo.InvariantCulture)}}}";
        }

        private string Output()
        {
            string text = global::Logger.GetOutputString();
            return $"{{\"output\":{Q(text)}}}";
        }

        private string ClearOutput()
        {
            global::Logger.Clear();
            return JsonOk("output cleared");
        }

        private string State()
        {
            var sim = MainSim.Inst;
            if (sim == null) return JsonError("MainSim not ready");

            // Unlocks  ──  { "unlockName": level, ... }
            var unlocks    = sim.GetUnlocks();
            var unlocksSb  = new StringBuilder("{");
            bool first     = true;
            foreach (var kv in unlocks)
            {
                if (!first) unlocksSb.Append(',');
                unlocksSb.Append(Q(kv.Key)).Append(':').Append(kv.Value);
                first = false;
            }
            unlocksSb.Append('}');

            // Items  ──  { "itemName": count, ... }
            var items    = sim.GetInventory();
            var itemsSb  = new StringBuilder("{");
            bool firstI  = true;
            for (int i = 0; i < items.items.Length; i++)
            {
                if (items.items[i] <= 0) continue;
                var itemSO = ResourceManager.GetItem(i);
                if (itemSO == null) continue;
                if (!firstI) itemsSb.Append(',');
                // ScriptableObject.name is the asset name (e.g. "Hay", "Wood")
                itemsSb.Append(Q(itemSO.name)).Append(':').Append(items.items[i].ToString("G"));
                firstI = false;
            }
            itemsSb.Append('}');

            var wsz  = sim.GetWorldSize();
            var time = sim.GetCurrentTime();
            return
                $"{{\"unlocks\":{unlocksSb}," +
                $"\"items\":{itemsSb}," +
                $"\"worldSize\":{wsz.x}," +
                $"\"timeSec\":{time.Seconds.ToString("F3", CultureInfo.InvariantCulture)}," +
                $"\"isExecuting\":{B(sim.IsExecuting())}," +
                $"\"isSimulating\":{B(sim.IsSimulating())}}}";
        }

        private string Scripts()
        {
            var sim = MainSim.Inst;
            if (sim == null) return JsonError("MainSim not ready");
            var sb    = new StringBuilder("[");
            bool first = true;
            foreach (var kv in sim.workspace.codeWindows)
            {
                if (!first) sb.Append(',');
                sb.Append($"{{\"name\":{Q(kv.Key)},\"isExecuting\":{B(kv.Value.isExecuting)}}}");
                first = false;
            }
            sb.Append(']');
            return $"{{\"scripts\":{sb}}}";
        }

        private string Run(string body)
        {
            var sim = MainSim.Inst;
            if (sim == null) return JsonError("MainSim not ready");

            // Parse { "script": "name" }  (simple extraction, no JSON dep needed)
            string scriptName = "main";
            var m = Regex.Match(body, @"""script""\s*:\s*""([^""]+)""");
            if (m.Success) scriptName = m.Groups[1].Value;

            if (!sim.workspace.codeWindows.TryGetValue(scriptName, out var cw))
                return JsonError($"Script '{scriptName}' is not open in the game");

            if (sim.IsExecuting())
                return JsonError("Already executing — call /api/stop first");

            // Parse then start
            var node = cw.Parse();
            if (node == null)
                return JsonError($"Parse failed for '{scriptName}' — check in-game error panel");

            sim.StartMainExecution(cw, node);
            return JsonOk($"Started '{scriptName}'");
        }

        private string Stop()
        {
            var sim = MainSim.Inst;
            if (sim == null) return JsonError("MainSim not ready");
            sim.StopMainExecution();
            return JsonOk("stopped");
        }

        private string Step()
        {
            var sim = MainSim.Inst;
            if (sim == null) return JsonError("MainSim not ready");
            sim.NextExecutionStep();
            return JsonOk("stepped");
        }

        /// <summary>
        /// GET /api/grid — full snapshot of the farm grid.
        /// Returns world size, drone positions, and every cell's entity/ground/water state.
        /// </summary>
        private string Grid()
        {
            if (!TryGetSimAndLock(out var simulation, out var lockObj))
                return JsonError("MainSim not ready");

            lock (lockObj)
            {
                // Re-read inside the lock (sim field could be swapped by leaderboard)
                simulation = (Simulation)_simField.GetValue(MainSim.Inst);
                var farm = simulation?.farm;
                if (farm == null) return JsonError("Farm not ready");

                var grid = farm.grid;
                var ws   = grid.WorldSize;
                var sb   = new StringBuilder(512);

                sb.Append("{\"world_size\":{\"x\":").Append(ws.x).Append(",\"y\":").Append(ws.y).Append("},");

                // Drones
                sb.Append("\"drones\":[");
                bool firstD = true;
                for (int di = 0; di < farm.drones.Count; di++)
                {
                    var d = farm.drones[di];
                    if (d == null) continue;
                    if (!firstD) sb.Append(',');
                    sb.Append("{\"id\":").Append(di)
                      .Append(",\"x\":").Append(d.pos.x)
                      .Append(",\"y\":").Append(d.pos.y).Append('}');
                    firstD = false;
                }
                sb.Append("],");

                // Cells
                sb.Append("\"cells\":[");
                bool firstC = true;
                for (int y = 0; y < ws.y; y++)
                {
                    for (int x = 0; x < ws.x; x++)
                    {
                        if (!firstC) sb.Append(',');
                        var pos = new Vector2Int(x, y);

                        sb.Append("{\"x\":").Append(x).Append(",\"y\":").Append(y);

                        // Entity
                        if (grid.entities.TryGetValue(pos, out var entity))
                        {
                            sb.Append(",\"entity\":").Append(Q(entity.objectSO.objectName));
                            if (entity is Growable g)
                            {
                                double pct = g.GrownPercent;
                                if (double.IsNaN(pct) || double.IsInfinity(pct)) pct = 0.0;
                                pct = Math.Min(pct, 1.0);
                                sb.Append(",\"grown_pct\":").Append(pct.ToString("F3", CultureInfo.InvariantCulture));
                                sb.Append(",\"harvestable\":").Append(B(g.Harvestable));
                            }
                        }
                        else
                        {
                            sb.Append(",\"entity\":null");
                        }

                        // Ground
                        if (grid.grounds.TryGetValue(pos, out var ground))
                            sb.Append(",\"ground\":").Append(Q(ground.objectSO.objectName));
                        else
                            sb.Append(",\"ground\":null");

                        // Water
                        sb.Append(",\"water\":").Append(grid.waterVolume[x, y].ToString("F3", CultureInfo.InvariantCulture));

                        sb.Append('}');
                        firstC = false;
                    }
                }
                sb.Append("]}");
                return sb.ToString();
            }
        }

        /// <summary>
        /// GET /api/shop — all currently purchasable upgrades with costs and affordability.
        /// Only returns unlocks whose parent prerequisites are met and that are not yet maxed.
        /// </summary>
        private string Shop()
        {
            if (!TryGetSimAndLock(out var simulation, out var lockObj))
                return JsonError("MainSim not ready");

            lock (lockObj)
            {
                simulation = (Simulation)_simField.GetValue(MainSim.Inst);
                var farm = simulation?.farm;
                if (farm == null) return JsonError("Farm not ready");

                var sb = new StringBuilder("[");
                bool first = true;

                foreach (var unlockSO in ResourceManager.GetAllUnlocks())
                {
                    if (!unlockSO.enabled) continue;

                    int currentLevel = farm.NumUnlocked(unlockSO);
                    if (currentLevel >= unlockSO.maxUnlockLevel) continue; // already maxed

                    // Parent must be unlocked for it to be purchasable
                    if (!string.IsNullOrEmpty(unlockSO.parentUnlock) && !farm.IsUnlocked(unlockSO.parentUnlock))
                        continue;

                    var cost = farm.GetUnlockCost(unlockSO);
                    bool canAfford = cost == null || cost.IsEmpty() || farm.Items.Contains(cost);

                    // Serialize cost items
                    var costSb = new StringBuilder("{");
                    bool firstCost = true;
                    if (cost != null && cost.items != null)
                    {
                        for (int i = 0; i < cost.items.Length; i++)
                        {
                            if (cost.items[i] <= 0) continue;
                            var itemSO = ResourceManager.GetItem(i);
                            if (itemSO == null) continue;
                            if (!firstCost) costSb.Append(',');
                            costSb.Append(Q(itemSO.name)).Append(':')
                                  .Append(cost.items[i].ToString("G", CultureInfo.InvariantCulture));
                            firstCost = false;
                        }
                    }
                    costSb.Append('}');

                    if (!first) sb.Append(',');
                    sb.Append('{');
                    sb.Append("\"name\":").Append(Q(unlockSO.unlockName));
                    sb.Append(",\"level\":").Append(currentLevel);
                    sb.Append(",\"max_level\":").Append(unlockSO.maxUnlockLevel);
                    sb.Append(",\"can_afford\":").Append(B(canAfford));
                    sb.Append(",\"cost\":").Append(costSb);
                    if (!string.IsNullOrEmpty(unlockSO.parentUnlock))
                        sb.Append(",\"parent\":").Append(Q(unlockSO.parentUnlock));
                    if (!string.IsNullOrEmpty(unlockSO.description))
                        sb.Append(",\"descr\":").Append(Q(unlockSO.description));
                    sb.Append('}');
                    first = false;
                }

                sb.Append(']');
                return sb.ToString();
            }
        }

        /// <summary>
        /// POST /api/buy  body: {"unlock": "unlock_name"}
        /// Purchases the next level of an upgrade using current inventory.
        /// </summary>
        private string Buy(string body)
        {
            var mainSim = MainSim.Inst;
            if (mainSim == null) return JsonError("MainSim not ready");

            var m = Regex.Match(body, @"""unlock""\s*:\s*""([^""]+)""");
            if (!m.Success) return JsonError("Missing 'unlock' field in body");
            string unlockName = m.Groups[1].Value;

            var unlockSO = ResourceManager.GetUnlock(unlockName);
            if (unlockSO == null) return JsonError($"Unknown unlock: '{unlockName}'");

            // UnlockOrUpgrade is already thread-safe (acquires lockSimulation internally)
            bool success = mainSim.UnlockOrUpgrade(unlockSO);
            if (success)
                return JsonOk($"Purchased '{unlockName}'");
            else
                return JsonError(
                    $"Cannot purchase '{unlockName}': already at max level, " +
                    "parent unlock not met, insufficient items, or simulation running.");
        }

        /// <summary>
        /// GET /api/docs — list all open DocsWindow instances with their content.
        /// </summary>
        private string GetDocs()
        {
            var sim = MainSim.Inst;
            if (sim == null) return JsonError("MainSim not ready");

            var sb    = new StringBuilder("[");
            bool first = true;

            foreach (var kv in sim.workspace.openWindows)
            {
                if (!kv.Value.TryGetComponent<DocsWindow>(out var dw)) continue;

                if (!first) sb.Append(',');
                sb.Append('{');
                sb.Append("\"window\":").Append(Q(kv.Key));
                sb.Append(",\"doc\":").Append(Q(dw.openDoc ?? ""));
                sb.Append(",\"content\":").Append(Q(dw.fullOpenDoc ?? ""));
                sb.Append('}');
                first = false;
            }

            sb.Append(']');
            return $"{{\"docs\":{sb}}}";
        }

        /// <summary>
        /// POST /api/docs/close  body: {"window": "docs0"}
        /// Closes a DocsWindow by its window name.
        /// </summary>
        private string CloseDocsWindow(string body)
        {
            var sim = MainSim.Inst;
            if (sim == null) return JsonError("MainSim not ready");

            var m = Regex.Match(body, @"""window""\s*:\s*""([^""]+)""");
            if (!m.Success) return JsonError("Missing 'window' field in body");
            string windowName = m.Groups[1].Value;

            if (!sim.workspace.openWindows.TryGetValue(windowName, out var window))
                return JsonError($"Window '{windowName}' not found");

            if (!window.TryGetComponent<DocsWindow>(out _))
                return JsonError($"Window '{windowName}' is not a docs window");

            window.Close();
            return JsonOk($"Closed docs window '{windowName}'");
        }

        /// <summary>
        /// GET /api/docs/fetch?path=docs/home.md
        /// Returns the text content of any game doc without opening a UI window.
        /// Replicates the DocsWindow.LoadDoc() text-building logic.
        /// </summary>
        private string FetchDoc(string query)
        {
            var m = Regex.Match(query, @"[?&]path=([^&]*)");
            if (!m.Success || string.IsNullOrEmpty(m.Groups[1].Value))
                return JsonError("Missing 'path' query parameter");

            string docPath = Uri.UnescapeDataString(m.Groups[1].Value);

            var sb = new StringBuilder();
            try
            {
                if (docPath.StartsWith("functions/"))
                {
                    string key = docPath.Substring("functions/".Length);
                    sb.Append(Localizer.Localize("code_tooltip_" + key) ?? "");
                }
                else if (docPath.StartsWith("unlocks/"))
                {
                    string unlockName = docPath.Substring("unlocks/".Length);
                    var info = TooltipUtils.UnlockTooltip(unlockName);
                    if (info == null) return JsonError($"Unlock tooltip not found: '{unlockName}'");
                    sb.Append(info.text ?? "");
                }
                else if (docPath.StartsWith("items/"))
                {
                    string itemName = docPath.Substring("items/".Length);
                    var info = TooltipUtils.ItemTooltip(itemName);
                    if (info == null) return JsonError($"Item tooltip not found: '{itemName}'");
                    sb.Append(info.text ?? "");
                }
                else if (docPath.StartsWith("objects/"))
                {
                    string objName = docPath.Substring("objects/".Length);
                    var info = TooltipUtils.FarmObjectTooltip(objName);
                    if (info == null) return JsonError($"Object tooltip not found: '{objName}'");
                    sb.Append(info.text ?? "");
                }
                else
                {
                    string text = Localizer.LoadDoc(docPath);
                    if (text == null) return JsonError($"Doc not found: '{docPath}'");
                    sb.Append(text);
                }
            }
            catch (Exception ex)
            {
                return JsonError($"Failed to fetch doc '{docPath}': {ex.Message}");
            }

            return $"{{\"path\":{Q(docPath)},\"content\":{Q(sb.ToString())}}}";
        }

        // ── Helpers ──────────────────────────────────────────────────────────

        private static string B(bool v) => v ? "true" : "false";

        private static string Q(string s) =>
            "\"" + (s ?? "").Replace("\\", "\\\\")
                            .Replace("\"", "\\\"")
                            .Replace("\n", "\\n")
                            .Replace("\r", "\\r") + "\"";

        private static string JsonOk(string msg)    => $"{{\"ok\":true,\"message\":{Q(msg)}}}";
        private static string JsonError(string msg) => $"{{\"ok\":false,\"error\":{Q(msg)}}}";

        private class MainThreadWork
        {
            public Func<string>          Handler;
            public string                Result;
            public ManualResetEventSlim  Done = new ManualResetEventSlim(false);
        }
    }
}
