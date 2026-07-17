import CoreFoundation
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
}
