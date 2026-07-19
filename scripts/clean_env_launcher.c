#include <errno.h>
#include <limits.h>
#include <mach-o/dyld.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sysexits.h>
#include <unistd.h>

static const char *const allowed_environment_keys[] = {
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "TMPDIR",
    "TMP",
    "TEMP",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "LC_MESSAGES",
    "LC_COLLATE",
    "LC_MONETARY",
    "LC_NUMERIC",
    "LC_TIME",
    "TZ",
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
    "XDG_CACHE_HOME",
    "XDG_STATE_HOME",
    "__CF_USER_TEXT_ENCODING",
    "XPC_SERVICE_NAME",
};

static void write_error(const char *message) {
    size_t remaining = strlen(message);
    while (remaining > 0) {
        ssize_t written = write(STDERR_FILENO, message, remaining);
        if (written < 0) {
            if (errno == EINTR) {
                continue;
            }
            return;
        }
        message += written;
        remaining -= (size_t)written;
    }
}

static char *environment_entry(const char *key, const char *value) {
    size_t key_length = strlen(key);
    size_t value_length = strlen(value);
    if (key_length > SIZE_MAX - value_length - 2) {
        return NULL;
    }
    size_t length = key_length + value_length + 2;
    char *entry = malloc(length);
    if (entry == NULL) {
        return NULL;
    }
    int result = snprintf(entry, length, "%s=%s", key, value);
    if (result < 0 || (size_t)result >= length) {
        free(entry);
        return NULL;
    }
    return entry;
}

static void free_environment(char **environment) {
    if (environment == NULL) {
        return;
    }
    for (size_t index = 0; environment[index] != NULL; index++) {
        free(environment[index]);
    }
    free(environment);
}

static char **sanitized_environment(void) {
    size_t key_count = sizeof(allowed_environment_keys) / sizeof(allowed_environment_keys[0]);
    char **environment = calloc(key_count + 1, sizeof(char *));
    if (environment == NULL) {
        return NULL;
    }
    size_t output_index = 0;
    for (size_t index = 0; index < key_count; index++) {
        const char *key = allowed_environment_keys[index];
        const char *value = getenv(key);
        if (value == NULL) {
            continue;
        }
        environment[output_index] = environment_entry(key, value);
        if (environment[output_index] == NULL) {
            free_environment(environment);
            return NULL;
        }
        output_index++;
    }
    return environment;
}

static int executable_path(char output[PATH_MAX]) {
    uint32_t size = PATH_MAX;
    if (_NSGetExecutablePath(output, &size) != 0) {
        return -1;
    }
    char resolved[PATH_MAX];
    if (realpath(output, resolved) == NULL) {
        return -1;
    }
    size_t length = strlen(resolved);
    if (length >= PATH_MAX) {
        return -1;
    }
    memcpy(output, resolved, length + 1);
    return 0;
}

static int target_path(const char *executable, char output[PATH_MAX]) {
    char directory[PATH_MAX];
    size_t executable_length = strlen(executable);
    if (executable_length >= sizeof(directory)) {
        return -1;
    }
    memcpy(directory, executable, executable_length + 1);
    char *separator = strrchr(directory, '/');
    if (separator == NULL || separator[1] == '\0') {
        return -1;
    }
    const char *role = separator + 1;
    *separator = '\0';

    const char *relative_target = NULL;
    if (strcmp(role, "OpenUsage Bar") == 0) {
        relative_target = "OpenUsage Bar.runtime";
    } else if (strcmp(role, "OpenUsage Collector") == 0) {
        relative_target =
            "../Helpers/OpenUsage Provider Settings.app/Contents/MacOS/"
            "OpenUsage Provider Settings";
    } else {
        return 1;
    }

    char candidate[PATH_MAX];
    int result = snprintf(candidate, sizeof(candidate), "%s/%s", directory, relative_target);
    if (result < 0 || (size_t)result >= sizeof(candidate)) {
        return -1;
    }
    if (realpath(candidate, output) == NULL || access(output, X_OK) != 0) {
        return -1;
    }
    return 0;
}

int main(int argc, char **argv) {
    char executable[PATH_MAX];
    char target[PATH_MAX];
    if (argc < 1 || argv == NULL || executable_path(executable) != 0) {
        write_error("openusage_clean_launcher_unavailable\n");
        return EX_OSERR;
    }
    int target_result = target_path(executable, target);
    if (target_result == 1) {
        write_error("openusage_clean_launcher_invalid_role\n");
        return EX_USAGE;
    }
    if (target_result != 0) {
        write_error("openusage_clean_launcher_target_unavailable\n");
        return EX_OSERR;
    }

    char **environment = sanitized_environment();
    char **child_arguments = calloc((size_t)argc + 1, sizeof(char *));
    if (environment == NULL || child_arguments == NULL) {
        free_environment(environment);
        free(child_arguments);
        write_error("openusage_clean_launcher_unavailable\n");
        return EX_OSERR;
    }
    child_arguments[0] = target;
    for (int index = 1; index < argc; index++) {
        child_arguments[index] = argv[index];
    }

    execve(target, child_arguments, environment);
    free_environment(environment);
    free(child_arguments);
    write_error("openusage_clean_launcher_exec_failed\n");
    return EX_OSERR;
}
