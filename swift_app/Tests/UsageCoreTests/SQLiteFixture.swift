import Foundation
import SQLite3

enum FixtureError: Error {
    case sqlite(String)
}

final class SQLiteFixture {
    let directory: URL
    let databaseURL: URL

    init(
        userVersion: Int32 = 1, malformedToken: Bool = false,
        providerInstancesSQL: String? = nil, extraSQL: String = ""
    ) throws {
        directory = FileManager.default.temporaryDirectory
            .appendingPathComponent("OpenUsageBarTests-\(UUID().uuidString)", isDirectory: true)
        databaseURL = directory.appendingPathComponent("ledger.sqlite")
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)

        var handle: OpaquePointer?
        guard sqlite3_open(databaseURL.path, &handle) == SQLITE_OK, let handle else {
            throw FixtureError.sqlite("fixture open failed")
        }
        defer { sqlite3_close(handle) }

        try execute(handle, Self.schema)
        if userVersion >= 2 {
            try execute(handle, Self.costSchema)
        }
        if userVersion >= 3 {
            try execute(handle, Self.sourceSchema)
        }
        if userVersion >= 4 {
            try execute(handle, Self.publicRevisionSchema)
        }
        try execute(handle, "PRAGMA user_version=\(userVersion)")
        try execute(handle, malformedToken ? Self.malformedRows : Self.rows)
        if userVersion >= 2 {
            try execute(handle, Self.costRows)
        }
        if let providerInstancesSQL {
            try execute(handle, Self.providerInstancesSchema)
            try execute(handle, providerInstancesSQL)
        }
        if !extraSQL.isEmpty { try execute(handle, extraSQL) }
    }

    deinit {
        try? FileManager.default.removeItem(at: directory)
    }

    private func execute(_ handle: OpaquePointer, _ sql: String) throws {
        var message: UnsafeMutablePointer<CChar>?
        let result = sqlite3_exec(handle, sql, nil, nil, &message)
        defer { sqlite3_free(message) }
        guard result == SQLITE_OK else {
            throw FixtureError.sqlite("fixture SQL failed")
        }
    }

    private static let schema = """
    CREATE TABLE daily_model_usage(
      day TEXT NOT NULL, provider_id TEXT NOT NULL, account_ref TEXT NOT NULL DEFAULT '',
      model_id TEXT NOT NULL, input_tokens INTEGER NOT NULL, output_tokens INTEGER NOT NULL,
      cache_read_tokens INTEGER NOT NULL, cache_creation_tokens INTEGER NOT NULL,
      reasoning_tokens INTEGER, total_tokens INTEGER NOT NULL, cost_amount TEXT,
      cost_currency TEXT, cost_basis TEXT, quality TEXT NOT NULL, imported_at TEXT NOT NULL,
      revision INTEGER NOT NULL, payload_hash TEXT NOT NULL,
      PRIMARY KEY(day,provider_id,account_ref,model_id));
    CREATE TABLE daily_coverage(
      day TEXT NOT NULL, provider_id TEXT NOT NULL, account_ref TEXT NOT NULL DEFAULT '',
      imported_at TEXT NOT NULL, PRIMARY KEY(day,provider_id,account_ref));
    CREATE TABLE quota_state(
      record_id TEXT PRIMARY KEY, observed_at TEXT NOT NULL, provider_id TEXT NOT NULL,
      account_ref TEXT NOT NULL DEFAULT '', quota_name TEXT NOT NULL, unit TEXT NOT NULL,
      used TEXT, quota_limit TEXT, remaining TEXT, remaining_ratio REAL, resets_at TEXT,
      period_start TEXT, period_end TEXT, state TEXT NOT NULL, quality TEXT NOT NULL,
      stale INTEGER NOT NULL, revision INTEGER NOT NULL, payload_hash TEXT NOT NULL);
    CREATE TABLE quota_snapshots(
      snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT, record_id TEXT NOT NULL,
      observed_at TEXT NOT NULL, provider_id TEXT NOT NULL,
      account_ref TEXT NOT NULL DEFAULT '', quota_name TEXT NOT NULL,
      payload_json TEXT NOT NULL, payload_hash TEXT NOT NULL);
    CREATE INDEX quota_snapshot_record_time ON quota_snapshots(record_id, observed_at DESC);
    CREATE INDEX quota_snapshot_provider_account_time
      ON quota_snapshots(provider_id, account_ref, observed_at DESC, snapshot_id DESC);
    CREATE TABLE source_status(
      provider_id TEXT NOT NULL, source_id TEXT NOT NULL, state TEXT NOT NULL,
      last_attempt_at TEXT NOT NULL, last_success_at TEXT, stale_at TEXT, error_code TEXT,
      PRIMARY KEY(provider_id,source_id));
    CREATE TABLE change_log(
      change_seq INTEGER PRIMARY KEY AUTOINCREMENT, record_type TEXT NOT NULL,
      record_id TEXT NOT NULL, revision INTEGER NOT NULL, operation TEXT NOT NULL,
      changed_at TEXT NOT NULL, payload_json TEXT, payload_hash TEXT NOT NULL);
    CREATE UNIQUE INDEX change_log_record_revision_unique
      ON change_log(record_type,record_id,revision);
    CREATE TABLE ledger_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
    """

    private static let costSchema = """
    CREATE TABLE daily_costs(
      day TEXT NOT NULL, provider_id TEXT NOT NULL, account_ref TEXT NOT NULL DEFAULT '',
      cost_kind TEXT NOT NULL, currency TEXT NOT NULL, amount TEXT NOT NULL,
      basis TEXT NOT NULL, quality TEXT NOT NULL, imported_at TEXT NOT NULL,
      revision INTEGER NOT NULL, payload_hash TEXT NOT NULL,
      PRIMARY KEY(day,provider_id,account_ref,cost_kind,currency));
    CREATE TABLE daily_cost_coverage(
      day TEXT NOT NULL, provider_id TEXT NOT NULL, account_ref TEXT NOT NULL DEFAULT '',
      imported_at TEXT NOT NULL, PRIMARY KEY(day,provider_id,account_ref));
    CREATE INDEX daily_cost_provider_account_day
      ON daily_costs(provider_id, account_ref, day);
    """

    private static let sourceSchema = """
    ALTER TABLE daily_model_usage
      ADD COLUMN source_id TEXT NOT NULL DEFAULT 'legacy';
    ALTER TABLE daily_coverage
      ADD COLUMN source_id TEXT NOT NULL DEFAULT 'legacy';
    """

    private static let publicRevisionSchema = """
    ALTER TABLE source_status
      ADD COLUMN revision INTEGER NOT NULL DEFAULT 1;
    ALTER TABLE source_status
      ADD COLUMN payload_hash TEXT NOT NULL DEFAULT '';
    """

    private static let costRows = """
    INSERT INTO daily_costs VALUES
      ('2026-07-01','openai','','actual','USD','0','provider_reported','direct','2026-07-02T00:05:00Z',1,'cost-zero'),
      ('2026-07-02','openai','','actual','USD','12.34','provider_reported','direct','2026-07-03T00:05:00Z',2,'cost-paid');
    INSERT INTO daily_cost_coverage VALUES
      ('2026-07-01','openai','','2026-07-02T00:05:00Z'),
      ('2026-07-02','openai','','2026-07-03T00:05:00Z'),
      ('2026-07-03','openai','','2026-07-04T00:05:00Z');
    """

    private static let rows = """
    INSERT INTO daily_model_usage(
      day,provider_id,account_ref,model_id,input_tokens,output_tokens,
      cache_read_tokens,cache_creation_tokens,reasoning_tokens,total_tokens,
      cost_amount,cost_currency,cost_basis,quality,imported_at,revision,payload_hash
    ) VALUES
      ('2026-07-02','codex','','gpt-5.5',40000000,4200000,30000000,0,NULL,74200000,
       '4.237100','USD','price_table_estimated','derived','2026-07-02T23:59:00Z',7,'daily-hash');
    INSERT INTO daily_coverage(day,provider_id,account_ref,imported_at) VALUES
      ('2026-07-01','codex','','2026-07-01T23:59:00Z'),
      ('2026-07-02','codex','','2026-07-02T23:59:00Z'),
      ('2026-07-03','codex','','2026-07-03T23:59:00Z'),
      ('2026-06-01','stepfun','','2026-06-01T23:59:00Z');
    INSERT INTO quota_state VALUES
      ('minimax.five-hour','2026-07-14T08:00:00Z','minimax','','5-hour','requests',
       '82','100','18',0.18,'2026-07-14T10:00:00Z','2026-07-14T05:00:00Z',
       '2026-07-14T10:00:00Z','ok','live',0,3,'quota-low'),
      ('minimax.monthly','2026-07-14T08:00:00Z','minimax','','monthly','tokens',
       NULL,NULL,NULL,0.75,'2026-08-01T00:00:00Z',NULL,NULL,'ok','derived',0,2,'quota-high'),
      ('codex.weekly','2026-07-14T07:00:00Z','codex','','weekly','tokens',
       NULL,NULL,NULL,NULL,NULL,NULL,NULL,'temporarily_unavailable','cached',1,9,'quota-null');
    INSERT INTO source_status(
      provider_id,source_id,state,last_attempt_at,last_success_at,stale_at,error_code
    ) VALUES
      ('cursor','openusage.daily','ok','2026-07-14T07:00:00Z','2026-07-14T07:00:00Z',
       '2026-07-14T07:05:00Z',NULL),
      ('codex','openusage.daily','ok','2026-07-14T08:58:00Z','2026-07-14T08:58:00Z',
       '2026-07-14T09:15:00Z',NULL),
      ('stepfun','openusage.daily','temporarily_unavailable','2026-07-14T08:30:00Z',NULL,NULL,
       'command_failed');
    INSERT INTO change_log(change_seq,record_type,record_id,revision,operation,changed_at,payload_json,payload_hash)
      VALUES(1842,'quota','minimax.five-hour',3,'update','2026-07-14T08:00:00Z',NULL,'change-hash');
    """

    private static let providerInstancesSchema = """
    CREATE TABLE provider_instances(
      provider_id TEXT PRIMARY KEY, family_id TEXT NOT NULL,
      display_name TEXT NOT NULL, category TEXT NOT NULL,
      credential_source TEXT NOT NULL, source_kind TEXT NOT NULL,
      observed_at TEXT NOT NULL, revision INTEGER NOT NULL,
      payload_hash TEXT NOT NULL);
    """

    static let productionProviderInstances = """
    INSERT INTO provider_instances VALUES
      ('minimax-main','minimax','MiniMax Main','subscription','minimax_builtin_api','builtin_api','2026-07-14T08:00:00Z',1,'mm'),
      ('step-plan-cn','step_plan','Step Plan CN','subscription','step_plan_official_api','official_api','2026-07-14T08:00:00Z',1,'step'),
      ('opencode','opencode','OpenCode','subscription','openusage','openusage','2026-07-14T08:00:00Z',1,'oc'),
      ('copilot','copilot','GitHub Copilot','subscription','openusage','openusage','2026-07-14T08:00:00Z',1,'gh'),
      ('gemini_cli','gemini_cli','Gemini CLI','subscription','openusage','openusage','2026-07-14T08:00:00Z',1,'gem'),
      ('qwen_cli','qwen_cli','Qwen CLI','local_tool','openusage','openusage','2026-07-14T08:00:00Z',1,'qwen'),
      ('ollama','ollama','Ollama','local_tool','openusage','openusage','2026-07-14T08:00:00Z',1,'ollama'),
      ('minimax-foo','minimax-foo','MiniMax Foo API','api','api_key','generic_https','2026-07-14T08:00:00Z',1,'generic');
    """

    static func providerInstanceRow(
        providerID: String = "codex", familyID: String = "codex",
        displayName: String = "Work Codex", category: String = "subscription",
        credentialSource: String = "openusage", sourceKind: String = "openusage"
    ) -> String {
        func sql(_ value: String) -> String {
            "'" + value.replacingOccurrences(of: "'", with: "''") + "'"
        }
        return """
        INSERT INTO provider_instances VALUES(
          \(sql(providerID)),\(sql(familyID)),\(sql(displayName)),\(sql(category)),
          \(sql(credentialSource)),\(sql(sourceKind)),'2026-07-14T08:00:00Z',1,'identity');
        """
    }

    private static let malformedRows = """
    INSERT INTO daily_model_usage(
      day,provider_id,account_ref,model_id,input_tokens,output_tokens,
      cache_read_tokens,cache_creation_tokens,reasoning_tokens,total_tokens,
      cost_amount,cost_currency,cost_basis,quality,imported_at,revision,payload_hash
    ) VALUES
      ('2026-07-02','codex','','gpt-5.5','not-an-integer',4200000,30000000,0,NULL,74200000,
       NULL,NULL,NULL,'derived','2026-07-02T23:59:00Z',7,'daily-hash');
    INSERT INTO daily_coverage(day,provider_id,account_ref,imported_at)
      VALUES('2026-07-02','codex','','2026-07-02T23:59:00Z');
    INSERT INTO change_log(change_seq,record_type,record_id,revision,operation,changed_at,payload_json,payload_hash)
      VALUES(1842,'daily','bad',1,'insert','2026-07-02T23:59:00Z',NULL,'change-hash');
    """
}

final class PythonActivityStoreFixture {
    let directory: URL
    let databaseURL: URL

    init() throws {
        directory = FileManager.default.temporaryDirectory
            .appendingPathComponent("OpenUsageBarPythonTests-\(UUID().uuidString)", isDirectory: true)
        databaseURL = directory.appendingPathComponent("ledger.sqlite")
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        let repositoryRoot = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
        let script = """
        import sys
        from openusage_bar.activity_store import ActivityStore, DailyUsageRow
        store = ActivityStore(sys.argv[1])
        store.replace_daily_usage('codex', '2026-07-02', [DailyUsageRow(
            day='2026-07-02', provider_id='codex', model_id='gpt-5.5',
            input_tokens=4, output_tokens=2, cache_read_tokens=3,
            cache_creation_tokens=0, reasoning_tokens=None, total_tokens=9,
            cost_amount='1.2500', cost_currency='USD', cost_basis='exact',
            quality='exact', imported_at='2026-07-02T23:59:00Z')],
            imported_at='2026-07-02T23:59:00Z')
        store.close()
        """
        let process = Process()
        process.executableURL = repositoryRoot.appendingPathComponent(".build-venv/bin/python")
        process.arguments = ["-c", script, databaseURL.path]
        process.currentDirectoryURL = repositoryRoot
        var environment = ProcessInfo.processInfo.environment
        environment["PYTHONPATH"] = repositoryRoot.path
        process.environment = environment
        try process.run()
        process.waitUntilExit()
        guard process.terminationStatus == 0 else { throw FixtureError.sqlite("python fixture failed") }
    }

    deinit { try? FileManager.default.removeItem(at: directory) }
}

final class PythonActiveActivityStoreFixture {
    let directory: URL
    let databaseURL: URL
    private let process = Process()
    private let input = Pipe()
    private let output = Pipe()

    init() throws {
        directory = FileManager.default.temporaryDirectory
            .appendingPathComponent("OpenUsageBarActivePythonTests-\(UUID().uuidString)", isDirectory: true)
        databaseURL = directory.appendingPathComponent("ledger.sqlite")
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        let repositoryRoot = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent()
            .deletingLastPathComponent().deletingLastPathComponent()
        let script = """
        import sys
        from openusage_bar.activity_store import ActivityStore, DailyUsageRow
        def row(day, tokens):
            return DailyUsageRow(day=day, provider_id='codex', model_id='gpt-5.5',
                input_tokens=tokens, output_tokens=0, cache_read_tokens=0,
                cache_creation_tokens=0, reasoning_tokens=None, total_tokens=tokens,
                cost_amount=None, cost_currency=None, cost_basis=None, quality='exact',
                imported_at=day + 'T23:59:00Z')
        store = ActivityStore(sys.argv[1])
        store.replace_daily_usage('codex', '2026-07-01', [row('2026-07-01', 1)],
            imported_at='2026-07-01T23:59:00Z')
        print('ready', flush=True)
        if sys.stdin.readline().strip() == 'refresh':
            store.replace_daily_usage('codex', '2026-07-02', [row('2026-07-02', 2)],
                imported_at='2026-07-02T23:59:00Z')
            print('refreshed', flush=True)
        sys.stdin.readline()
        store.close()
        """
        process.executableURL = repositoryRoot.appendingPathComponent(".build-venv/bin/python")
        process.arguments = ["-c", script, databaseURL.path]
        process.currentDirectoryURL = repositoryRoot
        var environment = ProcessInfo.processInfo.environment
        environment["PYTHONPATH"] = repositoryRoot.path
        process.environment = environment
        process.standardInput = input
        process.standardOutput = output
        try process.run()
        guard try readSignal() == "ready" else { throw FixtureError.sqlite("active fixture failed") }
    }

    func refresh() throws {
        try input.fileHandleForWriting.write(contentsOf: Data("refresh\n".utf8))
        guard try readSignal() == "refreshed" else { throw FixtureError.sqlite("refresh failed") }
    }

    func closeWriter() throws {
        if process.isRunning {
            try input.fileHandleForWriting.write(contentsOf: Data("close\n".utf8))
            process.waitUntilExit()
            guard process.terminationStatus == 0 else { throw FixtureError.sqlite("writer close failed") }
        }
    }

    private func readSignal() throws -> String {
        let data = output.fileHandleForReading.availableData
        guard let value = String(data: data, encoding: .utf8)?.trimmingCharacters(in: .whitespacesAndNewlines),
              !value.isEmpty else { throw FixtureError.sqlite("missing fixture signal") }
        return value
    }

    deinit {
        if process.isRunning {
            try? input.fileHandleForWriting.write(contentsOf: Data("close\n".utf8))
            process.waitUntilExit()
        }
        try? FileManager.default.removeItem(at: directory)
    }
}

final class PythonRestartedWriter {
    private let process = Process()
    private let input = Pipe()
    private let output = Pipe()

    init(databaseURL: URL, day: String, tokens: Int, malformed: Bool = false) throws {
        let repositoryRoot = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent()
            .deletingLastPathComponent().deletingLastPathComponent()
        let script = """
        import sys
        from openusage_bar.activity_store import ActivityStore, DailyUsageRow
        day, tokens = sys.argv[2], int(sys.argv[3])
        store = ActivityStore(sys.argv[1])
        store.replace_daily_usage('codex', day, [DailyUsageRow(
            day=day, provider_id='codex', model_id='gpt-5.5',
            input_tokens=tokens, output_tokens=0, cache_read_tokens=0,
            cache_creation_tokens=0, reasoning_tokens=None, total_tokens=tokens,
            cost_amount=None, cost_currency=None, cost_basis=None, quality='exact',
            imported_at=day + 'T23:59:00Z')], imported_at=day + 'T23:59:00Z')
        if sys.argv[4] == 'malformed':
            with store._connection:
                store._connection.execute(
                    "UPDATE daily_model_usage SET input_tokens='not-an-integer' WHERE day=?", (day,))
        print('refreshed', flush=True)
        sys.stdin.readline()
        store.close()
        """
        process.executableURL = repositoryRoot.appendingPathComponent(".build-venv/bin/python")
        process.arguments = [
            "-c", script, databaseURL.path, day, String(tokens), malformed ? "malformed" : "canonical",
        ]
        process.currentDirectoryURL = repositoryRoot
        var environment = ProcessInfo.processInfo.environment
        environment["PYTHONPATH"] = repositoryRoot.path
        process.environment = environment
        process.standardInput = input
        process.standardOutput = output
        try process.run()
        let signal = output.fileHandleForReading.availableData
        guard String(data: signal, encoding: .utf8)?.trimmingCharacters(in: .whitespacesAndNewlines) == "refreshed" else {
            throw FixtureError.sqlite("restarted writer failed")
        }
    }

    func close() throws {
        if process.isRunning {
            try input.fileHandleForWriting.write(contentsOf: Data("close\n".utf8))
            process.waitUntilExit()
            guard process.terminationStatus == 0 else { throw FixtureError.sqlite("restarted writer close failed") }
        }
    }

    deinit {
        if process.isRunning {
            try? input.fileHandleForWriting.write(contentsOf: Data("close\n".utf8))
            process.waitUntilExit()
        }
    }
}
