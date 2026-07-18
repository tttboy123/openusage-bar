import Foundation
import SQLite3

extension UsageRepository {
    func validateSchema(_ database: OpaquePointer) throws {
        let version = try scalarInt64(database, sql: "PRAGMA user_version")
        guard (1...5).contains(version) else { throw RepositoryError.incompatibleSchema }
        let expected = GeneratedActivitySchema.expectedTables(version: version)
        for (table, signature) in expected {
            let actual = try queryColumnSignature(database, table: table)
            guard actual == signature else {
                throw RepositoryError.incompatibleSchema
            }
        }
        try validateIndexes(database, includeCosts: version >= 2)
        try validateAutoincrement(database)
    }

    func validateIndexes(
        _ database: OpaquePointer, includeCosts: Bool
    ) throws {
        let expected = GeneratedActivitySchema.expectedIndexes(includeCosts: includeCosts)
        for contract in expected {
            let properties: (unique: Bool, partial: Bool) = try withStatement(
                database,
                sql: "SELECT \"unique\",partial FROM pragma_index_list(?) WHERE name=?",
                bindings: [contract.table, contract.name]
            ) { statement in
                guard sqlite3_step(statement) == SQLITE_ROW else { throw RepositoryError.incompatibleSchema }
                let value = (try requiredBool(statement, 0), try requiredBool(statement, 1))
                guard sqlite3_step(statement) == SQLITE_DONE else { throw RepositoryError.incompatibleSchema }
                return value
            }
            guard properties.unique == contract.unique, properties.partial == contract.partial else {
                throw RepositoryError.incompatibleSchema
            }
            let terms: [IndexTerm] = try withStatement(
                database,
                sql: "SELECT name,desc FROM pragma_index_xinfo(?) WHERE key=1 ORDER BY seqno",
                bindings: [contract.name]
            ) { statement in
                var values: [IndexTerm] = []
                while true {
                    switch sqlite3_step(statement) {
                    case SQLITE_ROW:
                        values.append(IndexTerm(
                            name: try requiredText(statement, 0),
                            descending: try requiredBool(statement, 1)
                        ))
                    case SQLITE_DONE: return values
                    default: throw RepositoryError.incompatibleSchema
                    }
                }
            }
            guard terms == contract.terms else { throw RepositoryError.incompatibleSchema }
        }
    }

    func validateAutoincrement(_ database: OpaquePointer) throws {
        let contracts = GeneratedActivitySchema.autoincrementColumns
        for (table, column) in contracts {
            let definition = try withStatement(
                database,
                sql: "SELECT sql FROM sqlite_master WHERE type=? AND name=?",
                bindings: ["table", table]
            ) { statement in
                guard sqlite3_step(statement) == SQLITE_ROW else { throw RepositoryError.incompatibleSchema }
                let value = try requiredText(statement, 0)
                guard sqlite3_step(statement) == SQLITE_DONE else { throw RepositoryError.incompatibleSchema }
                return value
            }
            guard SQLiteDDLParser.hasCanonicalAutoincrementColumn(
                in: definition, column: column
            ) else { throw RepositoryError.incompatibleSchema }
        }
    }

    func queryColumnSignature(_ database: OpaquePointer, table: String) throws -> [SchemaColumn] {
        try withStatement(
            database,
            sql: "SELECT name,type,\"notnull\",dflt_value,pk FROM pragma_table_info(?) ORDER BY cid",
            bindings: [table]
        ) { statement in
            var result: [SchemaColumn] = []
            while true {
                switch sqlite3_step(statement) {
                case SQLITE_ROW:
                    result.append(SchemaColumn(
                        name: try requiredText(statement, 0),
                        type: try requiredText(statement, 1).uppercased(),
                        required: try requiredBool(statement, 2),
                        defaultValue: try optionalText(statement, 3),
                        primaryKey: Int(try requiredInt64(statement, 4))
                    ))
                case SQLITE_DONE: return result
                default: throw RepositoryError.incompatibleSchema
                }
            }
        }
    }

}

struct SchemaColumn: Equatable {
    let name: String
    let type: String
    let required: Bool
    let defaultValue: String?
    let primaryKey: Int

    static func required(
        _ name: String, _ type: String, defaultValue: String? = nil, primaryKey: Int = 0
    ) -> Self {
        Self(name: name, type: type, required: true, defaultValue: defaultValue, primaryKey: primaryKey)
    }

    static func optional(
        _ name: String, _ type: String, defaultValue: String? = nil, primaryKey: Int = 0
    ) -> Self {
        Self(name: name, type: type, required: false, defaultValue: defaultValue, primaryKey: primaryKey)
    }
}

struct IndexTerm: Equatable {
    let name: String
    let descending: Bool
}

struct ExpectedIndex {
    let name: String
    let table: String
    let unique: Bool
    let partial: Bool
    let terms: [IndexTerm]
}

enum DDLToken: Equatable {
    case word(String)
    case symbol(Character)
}

enum SQLiteDDLParser {
    static func hasCanonicalAutoincrementColumn(in sql: String, column: String) -> Bool {
        let tokens = tokenize(sql)
        guard let opening = tokens.firstIndex(of: .symbol("(")) else { return false }
        var depth = 1
        var entry: [DDLToken] = []
        var entries: [[DDLToken]] = []
        for token in tokens[tokens.index(after: opening)...] {
            switch token {
            case .symbol("("):
                depth += 1
                entry.append(token)
            case .symbol(")"):
                depth -= 1
                if depth == 0 {
                    if !entry.isEmpty { entries.append(entry) }
                    break
                }
                entry.append(token)
            case .symbol(",") where depth == 1:
                entries.append(entry)
                entry.removeAll(keepingCapacity: true)
            default:
                entry.append(token)
            }
            if depth == 0 { break }
        }
        let expected: [DDLToken] = [
            .word(column.uppercased()), .word("INTEGER"), .word("PRIMARY"),
            .word("KEY"), .word("AUTOINCREMENT"),
        ]
        return entries.contains { entry in
            entry.count >= expected.count && Array(entry.prefix(expected.count)) == expected
        }
    }

    private static func tokenize(_ sql: String) -> [DDLToken] {
        let characters = Array(sql)
        var tokens: [DDLToken] = []
        var word = ""
        var index = 0

        func appendWord() {
            if !word.isEmpty {
                tokens.append(.word(word.uppercased()))
                word.removeAll(keepingCapacity: true)
            }
        }

        while index < characters.count {
            let character = characters[index]
            let next = index + 1 < characters.count ? characters[index + 1] : nil
            if character.isWhitespace {
                appendWord()
                index += 1
            } else if character == "-", next == "-" {
                appendWord()
                index += 2
                while index < characters.count, characters[index] != "\n" { index += 1 }
            } else if character == "/", next == "*" {
                appendWord()
                index += 2
                while index + 1 < characters.count,
                      !(characters[index] == "*" && characters[index + 1] == "/") {
                    index += 1
                }
                index = min(characters.count, index + 2)
            } else if character == "'" {
                appendWord()
                index += 1
                while index < characters.count {
                    if characters[index] == "'" {
                        if index + 1 < characters.count, characters[index + 1] == "'" {
                            index += 2
                            continue
                        }
                        index += 1
                        break
                    }
                    index += 1
                }
            } else if character == "\"" || character == "`" || character == "[" {
                appendWord()
                let closing: Character = character == "[" ? "]" : character
                var identifier = ""
                index += 1
                while index < characters.count {
                    if characters[index] == closing {
                        if closing != "]", index + 1 < characters.count,
                           characters[index + 1] == closing {
                            identifier.append(closing)
                            index += 2
                            continue
                        }
                        index += 1
                        break
                    }
                    identifier.append(characters[index])
                    index += 1
                }
                tokens.append(.word(identifier.uppercased()))
            } else if character == "(" || character == ")" || character == "," {
                appendWord()
                tokens.append(.symbol(character))
                index += 1
            } else if character.isLetter || character.isNumber || character == "_" {
                word.append(character)
                index += 1
            } else {
                appendWord()
                tokens.append(.symbol(character))
                index += 1
            }
        }
        appendWord()
        return tokens
    }
}
