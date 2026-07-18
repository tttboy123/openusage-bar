import CoreFoundation
import Darwin
import Foundation

public enum VisibilityStoreError: Error, Sendable, Hashable {
    case invalidSchema
    case tooLarge
    case unreadable
}

public struct VisibilitySnapshot: Sendable, Hashable {
    public let hiddenProviderIDs: Set<String>
    public let revision: UInt64

    public init(hiddenProviderIDs: Set<String>, revision: UInt64) {
        self.hiddenProviderIDs = hiddenProviderIDs
        self.revision = revision
    }
}

public struct VisibilityStore: Sendable {
    public static let maximumBytes = 64 * 1_024
    public let url: URL

    public init(url: URL) { self.url = url }

    public func load() throws -> VisibilitySnapshot {
        guard FileManager.default.fileExists(atPath: url.path) else {
            return VisibilitySnapshot(hiddenProviderIDs: [], revision: 0)
        }
        let handle: FileHandle
        do { handle = try FileHandle(forReadingFrom: url) }
        catch { throw VisibilityStoreError.unreadable }
        defer { try? handle.close() }
        var data = Data()
        do {
            while data.count <= Self.maximumBytes {
                let remaining = Self.maximumBytes + 1 - data.count
                guard let chunk = try handle.read(upToCount: min(8_192, remaining)), !chunk.isEmpty else { break }
                data.append(chunk)
            }
        } catch { throw VisibilityStoreError.unreadable }
        return try Self.decode(data)
    }

    public func save(hiddenProviderIDs: Set<String>) throws {
        guard hiddenProviderIDs.allSatisfy(Self.isStableID) else {
            throw VisibilityStoreError.invalidSchema
        }
        let payload: [String: Any] = [
            "version": 1,
            "hidden_provider_ids": hiddenProviderIDs.sorted(),
        ]
        guard let data = try? JSONSerialization.data(
            withJSONObject: payload, options: [.prettyPrinted, .sortedKeys]
        ), data.count <= Self.maximumBytes else {
            throw VisibilityStoreError.tooLarge
        }
        let directory = url.deletingLastPathComponent()
        do {
            try FileManager.default.createDirectory(
                at: directory, withIntermediateDirectories: true
            )
            try FileManager.default.setAttributes(
                [.posixPermissions: 0o700], ofItemAtPath: directory.path
            )
        } catch { throw VisibilityStoreError.unreadable }
        let temporary = directory.appendingPathComponent(
            ".visibility.\(UUID().uuidString).json"
        )
        let descriptor = open(
            temporary.path, O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC, 0o600
        )
        guard descriptor >= 0 else { throw VisibilityStoreError.unreadable }
        var succeeded = false
        defer {
            close(descriptor)
            if !succeeded { unlink(temporary.path) }
        }
        let wrote = data.withUnsafeBytes { bytes -> Bool in
            guard let base = bytes.baseAddress else { return data.isEmpty }
            var offset = 0
            while offset < data.count {
                let count = Darwin.write(
                    descriptor, base.advanced(by: offset), data.count - offset
                )
                if count > 0 { offset += count }
                else if count < 0, errno == EINTR { continue }
                else { return false }
            }
            return true
        }
        guard wrote, fsync(descriptor) == 0,
              rename(temporary.path, url.path) == 0
        else { throw VisibilityStoreError.unreadable }
        succeeded = true
    }

    public static func decode(_ data: Data) throws -> VisibilitySnapshot {
        guard data.count <= maximumBytes else { throw VisibilityStoreError.tooLarge }
        guard let payload = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let version = payload["version"] as? NSNumber,
              CFGetTypeID(version) != CFBooleanGetTypeID(),
              version.intValue == 1, version.doubleValue == 1,
              let values = payload["hidden_provider_ids"] as? [Any]
        else { throw VisibilityStoreError.invalidSchema }
        var hidden = Set<String>()
        for value in values {
            guard let providerID = value as? String,
                  providerID.range(of: #"^[A-Za-z0-9._-]+$"#, options: .regularExpression) != nil,
                  hidden.insert(providerID).inserted
            else { throw VisibilityStoreError.invalidSchema }
        }
        return VisibilitySnapshot(hiddenProviderIDs: hidden, revision: fnv1a(data))
    }

    private static func fnv1a(_ data: Data) -> UInt64 {
        data.reduce(UInt64(14_695_981_039_346_656_037)) { value, byte in
            (value ^ UInt64(byte)) &* 1_099_511_628_211
        }
    }

    private static func isStableID(_ value: String) -> Bool {
        !value.isEmpty && value.utf8.count <= 128
            && value.range(
                of: #"^[A-Za-z0-9._-]+$"#, options: .regularExpression
            ) != nil
    }
}
