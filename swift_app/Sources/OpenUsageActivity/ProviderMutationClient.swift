import Darwin
import Foundation

struct ProviderMutationLimits: Sendable, Equatable {
    let timeout: Duration
    let maximumResponseBytes: Int

    static let production = Self(
        timeout: .seconds(30), maximumResponseBytes: 131_072
    )
}

struct ProviderMutationClient: Sendable {
    let limits: ProviderMutationLimits

    init(limits: ProviderMutationLimits = .production) {
        self.limits = limits
    }

    func submit<Request: Encodable & Sendable>(
        _ request: Request,
        command: ProviderMutationCommand
    ) async -> Result<ProviderMutationResponse, ProviderMutationFailure> {
        await Task.detached(priority: .userInitiated) {
            guard limits.maximumResponseBytes > 0,
                  let requestData = try? JSONEncoder().encode(request),
                  requestData.count <= 131_072
            else { return .failure(.invalidResponse) }

            switch ProviderMutationProcessRunner().run(
                requestData, command: command, limits: limits
            ) {
            case let .failure(failure):
                return .failure(failure)
            case let .success(responseData):
                guard String(data: responseData, encoding: .utf8) != nil,
                      let response = try? JSONDecoder().decode(
                          ProviderMutationResponse.self, from: responseData
                      ),
                      response.version == 1
                else { return .failure(.invalidResponse) }
                return .success(response)
            }
        }.value
    }
}

private struct ProviderMutationProcessRunner: Sendable {
    func run(
        _ input: Data,
        command: ProviderMutationCommand,
        limits: ProviderMutationLimits
    ) -> Result<Data, ProviderMutationFailure> {
        var inputPipe = [Int32](repeating: -1, count: 2)
        var outputPipe = [Int32](repeating: -1, count: 2)
        guard pipe(&inputPipe) == 0 else { return .failure(.couldNotLaunch) }
        guard pipe(&outputPipe) == 0 else {
            close(inputPipe[0])
            close(inputPipe[1])
            return .failure(.couldNotLaunch)
        }
        defer {
            inputPipe.filter { $0 >= 0 }.forEach { close($0) }
            outputPipe.filter { $0 >= 0 }.forEach { close($0) }
        }

        guard setCloseOnExec(inputPipe + outputPipe),
              setNonblocking(inputPipe[1]), setNonblocking(outputPipe[0]),
              let pid = spawn(
                  command, inputRead: inputPipe[0], inputWrite: inputPipe[1],
                  outputRead: outputPipe[0], outputWrite: outputPipe[1]
              )
        else { return .failure(.couldNotLaunch) }

        closeAndInvalidate(&inputPipe[0])
        closeAndInvalidate(&outputPipe[1])
        let deadline = ContinuousClock.now.advanced(by: limits.timeout)
        var inputOffset = 0
        var response = Data()
        var status = Int32()

        while ContinuousClock.now < deadline {
            if inputPipe[1] >= 0 {
                let wrote = input.withUnsafeBytes { bytes -> Int in
                    guard let base = bytes.baseAddress else { return 0 }
                    return Darwin.write(
                        inputPipe[1], base.advanced(by: inputOffset), input.count - inputOffset
                    )
                }
                if wrote > 0 { inputOffset += wrote }
                if inputOffset == input.count {
                    closeAndInvalidate(&inputPipe[1])
                } else if wrote < 0, errno != EAGAIN, errno != EINTR {
                    terminateAndReap(pid)
                    return .failure(.couldNotLaunch)
                }
            }

            switch readAvailable(outputPipe[0], into: &response, limit: limits.maximumResponseBytes) {
            case .tooLarge:
                terminateAndReap(pid)
                return .failure(.responseTooLarge)
            case .closed:
                closeAndInvalidate(&outputPipe[0])
            case .available:
                break
            }

            let waited = waitpid(pid, &status, WNOHANG)
            if waited == pid {
                if outputPipe[0] >= 0 {
                    switch readAvailable(
                        outputPipe[0], into: &response, limit: limits.maximumResponseBytes
                    ) {
                    case .tooLarge: return .failure(.responseTooLarge)
                    case .available, .closed: break
                    }
                }
                return exitedSuccessfully(status)
                    ? .success(response) : .failure(.couldNotLaunch)
            }
            if waited == -1, errno != EINTR {
                return .failure(.couldNotLaunch)
            }
            Thread.sleep(forTimeInterval: 0.005)
        }

        terminateAndReap(pid)
        return .failure(.timedOut)
    }

    private enum ReadState { case available, closed, tooLarge }

    private func readAvailable(
        _ descriptor: Int32, into response: inout Data, limit: Int
    ) -> ReadState {
        guard descriptor >= 0 else { return .closed }
        var buffer = [UInt8](repeating: 0, count: 8_192)
        while true {
            let count = Darwin.read(descriptor, &buffer, buffer.count)
            if count > 0 {
                response.append(buffer, count: count)
                if response.count > limit { return .tooLarge }
            } else if count == 0 {
                return .closed
            } else if errno == EINTR {
                continue
            } else if errno == EAGAIN || errno == EWOULDBLOCK {
                return .available
            } else {
                return .closed
            }
        }
    }

    private func spawn(
        _ command: ProviderMutationCommand,
        inputRead: Int32, inputWrite: Int32,
        outputRead: Int32, outputWrite: Int32
    ) -> pid_t? {
        var attributes: posix_spawnattr_t?
        guard posix_spawnattr_init(&attributes) == 0 else { return nil }
        defer { posix_spawnattr_destroy(&attributes) }
        let flags = Int16(POSIX_SPAWN_SETPGROUP | POSIX_SPAWN_CLOEXEC_DEFAULT)
        guard posix_spawnattr_setflags(&attributes, flags) == 0,
              posix_spawnattr_setpgroup(&attributes, 0) == 0
        else { return nil }

        var actions: posix_spawn_file_actions_t?
        guard posix_spawn_file_actions_init(&actions) == 0 else { return nil }
        defer { posix_spawn_file_actions_destroy(&actions) }
        guard posix_spawn_file_actions_adddup2(&actions, inputRead, STDIN_FILENO) == 0,
              posix_spawn_file_actions_adddup2(&actions, outputWrite, STDOUT_FILENO) == 0,
              posix_spawn_file_actions_addopen(
                  &actions, STDERR_FILENO, "/dev/null", O_WRONLY, 0
              ) == 0,
              posix_spawn_file_actions_addclose(&actions, inputWrite) == 0,
              posix_spawn_file_actions_addclose(&actions, outputRead) == 0
        else { return nil }

        let arguments = [command.executableURL.path] + command.arguments
        let environment = ProcessInfo.processInfo.environment.sorted { $0.key < $1.key }
            .map { "\($0.key)=\($0.value)" }
        return withCStringArray(arguments) { argv in
            withCStringArray(environment) { envp in
                var pid = pid_t()
                let result = posix_spawn(
                    &pid, command.executableURL.path, &actions, &attributes, argv, envp
                )
                return result == 0 ? pid : nil
            }
        }
    }

    private func setCloseOnExec(_ descriptors: [Int32]) -> Bool {
        descriptors.allSatisfy { fcntl($0, F_SETFD, FD_CLOEXEC) == 0 }
    }

    private func setNonblocking(_ descriptor: Int32) -> Bool {
        let flags = fcntl(descriptor, F_GETFL)
        return flags >= 0 && fcntl(descriptor, F_SETFL, flags | O_NONBLOCK) == 0
    }

    private func closeAndInvalidate(_ descriptor: inout Int32) {
        guard descriptor >= 0 else { return }
        close(descriptor)
        descriptor = -1
    }

    private func terminateAndReap(_ pid: pid_t) {
        if getpgid(pid) == pid {
            Darwin.kill(-pid, SIGTERM)
        } else {
            Darwin.kill(pid, SIGTERM)
        }
        let grace = Date().addingTimeInterval(0.2)
        var status = Int32()
        while Date() < grace {
            let result = waitpid(pid, &status, WNOHANG)
            if result == pid || (result == -1 && errno != EINTR) { return }
            Thread.sleep(forTimeInterval: 0.005)
        }
        if getpgid(pid) == pid {
            Darwin.kill(-pid, SIGKILL)
        } else {
            Darwin.kill(pid, SIGKILL)
        }
        while waitpid(pid, &status, 0) == -1, errno == EINTR {}
    }

    private func exitedSuccessfully(_ status: Int32) -> Bool {
        (status & 0x7f) == 0 && ((status >> 8) & 0xff) == 0
    }

    private func withCStringArray<T>(
        _ values: [String],
        body: (UnsafeMutablePointer<UnsafeMutablePointer<CChar>?>) -> T
    ) -> T {
        let strings = values.map { value in value.withCString { strdup($0) } }
        defer { strings.forEach { free($0) } }
        var pointers: [UnsafeMutablePointer<CChar>?] = strings
        pointers.append(nil)
        return pointers.withUnsafeMutableBufferPointer { body($0.baseAddress!) }
    }
}
