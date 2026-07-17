#include <errno.h>
#include <stdio.h>

int main(int argc, char **argv) {
    if (argc != 3) {
        fputs("usage: atomic-swap <current> <staged>\n", stderr);
        return 64;
    }
    if (renamex_np(argv[1], argv[2], RENAME_SWAP) != 0) {
        int error = errno;
        perror("atomic-swap");
        return error == 0 ? 1 : error;
    }
    return 0;
}
