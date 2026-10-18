#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <linux/fs.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <unistd.h>

static void usage(const char *program) {
  fprintf(stderr,
          "usage: %s LABEL PATH BYTES [UNLINK_AFTER_BYTES [cleanup [REASSIGN_TARGET]]]\n",
          program);
}

static int cleanup_file(const char *label, const char *path) {
  errno = 0;
  int cleanup_rc = unlink(path);
  int cleanup_errno = cleanup_rc == -1 ? errno : 0;
  printf("CLEANUP|%s|%d\n", label, cleanup_rc);
  if (cleanup_rc != 0) {
    fprintf(stderr, "cleanup unlink %s: %s\n", path,
            strerror(cleanup_errno));
    return -1;
  }
  return 0;
}

static int effective_capabilities(uint64_t *result) {
  FILE *status = fopen("/proc/self/status", "r");
  if (!status) return -1;
  char line[256];
  int found = 0;
  while (fgets(line, sizeof(line), status)) {
    if (sscanf(line, "CapEff:%" SCNx64, result) == 1) {
      found = 1;
      break;
    }
  }
  if (ferror(status) || fclose(status) != 0 || !found) return -1;
  return 0;
}

static int read_single_mapping(const char *path, uint64_t *inside,
                               uint64_t *outside, uint64_t *count) {
  FILE *mapping = fopen(path, "r");
  if (!mapping) return -1;
  char line[256];
  char extra;
  int valid = fgets(line, sizeof(line), mapping) != NULL &&
              sscanf(line, " %" SCNu64 " %" SCNu64 " %" SCNu64 " %c",
                     inside, outside, count, &extra) == 3 &&
              *count > 0 && fgets(line, sizeof(line), mapping) == NULL &&
              !ferror(mapping);
  if (fclose(mapping) != 0) valid = 0;
  return valid ? 0 : -1;
}

int main(int argc, char **argv) {
  if (argc < 4 || argc > 7) {
    usage(argv[0]);
    return 64;
  }
  const char *label = argv[1];
  const char *path = argv[2];
  char *end = NULL;
  errno = 0;
  uint64_t requested = strtoull(argv[3], &end, 10);
  if (errno || !end || *end) {
    usage(argv[0]);
    return 64;
  }
  uint64_t unlink_after = UINT64_MAX;
  if (argc == 5) {
    errno = 0;
    unlink_after = strtoull(argv[4], &end, 10);
    if (errno || !end || *end) {
      usage(argv[0]);
      return 64;
    }
  }
  int final_cleanup = 0;
  const char *reassign_target = NULL;
  if (argc >= 6) {
    errno = 0;
    unlink_after = strtoull(argv[4], &end, 10);
    if (errno || !end || *end || strcmp(argv[5], "cleanup") != 0 ||
        (argc == 7 && argv[6][0] == '\0')) {
      usage(argv[0]);
      return 64;
    }
    final_cleanup = 1;
    if (argc == 7) reassign_target = argv[6];
  }

  uint64_t cap_eff;
  uint64_t uid_inside, uid_outside, uid_count;
  uint64_t gid_inside, gid_outside, gid_count;
  if (effective_capabilities(&cap_eff) != 0) {
    fprintf(stderr, "read CapEff: %s\n", strerror(errno));
    return 1;
  }
  if (read_single_mapping("/proc/self/uid_map", &uid_inside, &uid_outside,
                          &uid_count) != 0 ||
      read_single_mapping("/proc/self/gid_map", &gid_inside, &gid_outside,
                          &gid_count) != 0) {
    fprintf(stderr, "require exactly one valid UID and GID mapping\n");
    return 1;
  }
  if (final_cleanup) {
    printf("TARGET|%s|%s|%" PRIu64 "\n", label, path, requested);
  }
  printf("IDENTITY|%s|%u|%u|%" PRIu64 "\n", label, (unsigned)getuid(),
         (unsigned)getgid(), cap_eff);
  printf("USERNS|%s|%" PRIu64 "|%" PRIu64 "|%" PRIu64 "|%" PRIu64
         "|%" PRIu64 "|%" PRIu64 "\n",
         label, uid_inside, uid_outside, uid_count, gid_inside, gid_outside,
         gid_count);

  if (reassign_target) {
    int target_fd = open(reassign_target, O_RDONLY | O_CLOEXEC);
    if (target_fd < 0) {
      fprintf(stderr, "open reassignment target %s: %s\n", reassign_target,
              strerror(errno));
      return 1;
    }
    struct stat target_stat = {0};
    errno = 0;
    int target_stat_rc = fstat(target_fd, &target_stat);
    if (target_stat_rc != 0 || target_stat.st_uid != getuid()) {
      int target_errno = errno;
      if (target_stat_rc != 0) {
        fprintf(stderr, "fstat reassignment target %s: %s\n", reassign_target,
                strerror(target_errno));
      } else {
        fprintf(stderr, "reassignment target %s is owned by uid %u, expected %u\n",
                reassign_target, (unsigned)target_stat.st_uid,
                (unsigned)getuid());
      }
      close(target_fd);
      return 1;
    }
    struct fsxattr before = {0};
    struct fsxattr requested_attr;
    struct fsxattr after = {0};
    if (ioctl(target_fd, FS_IOC_FSGETXATTR, &before) != 0) {
      fprintf(stderr, "FS_IOC_FSGETXATTR before %s: %s\n", reassign_target,
              strerror(errno));
      close(target_fd);
      return 1;
    }
    requested_attr = before;
    requested_attr.fsx_projid = 0;
    errno = 0;
    int reassign_rc = ioctl(target_fd, FS_IOC_FSSETXATTR, &requested_attr);
    int reassign_errno = reassign_rc == -1 ? errno : 0;
    if (ioctl(target_fd, FS_IOC_FSGETXATTR, &after) != 0) {
      fprintf(stderr, "FS_IOC_FSGETXATTR after %s: %s\n", reassign_target,
              strerror(errno));
      close(target_fd);
      return 1;
    }
    printf("REASSIGN_TARGET|%s|%s\n", label, reassign_target);
    printf("REASSIGN|%s|%d|%d|%u|%u\n", label, reassign_rc,
           reassign_errno, before.fsx_projid, after.fsx_projid);
    if (close(target_fd) != 0) {
      fprintf(stderr, "close reassignment target %s: %s\n", reassign_target,
              strerror(errno));
      return 1;
    }
  }

  int open_flags = O_WRONLY | O_CREAT | O_CLOEXEC;
  open_flags |= final_cleanup ? O_EXCL : O_TRUNC;
  int fd = open(path, open_flags, 0600);
  if (fd < 0) {
    int open_errno = errno;
    if (final_cleanup && (open_errno == ENOSPC || open_errno == EDQUOT)) {
      struct stat absent = {0};
      errno = 0;
      if (lstat(path, &absent) == 0 || errno != ENOENT) {
        fprintf(stderr, "probe path %s exists or cannot be checked after open: %s\n",
                path, errno ? strerror(errno) : "path exists");
        return 1;
      }
      printf("RESULT|%s|0|0|%d|0|0|0|%u|%" PRIu64 "|0\n", label,
             open_errno, (unsigned)getuid(), cap_eff);
      printf("CLEANUP|%s|absent\n", label);
      return 0;
    }
    fprintf(stderr, "open %s: %s\n", path, strerror(open_errno));
    return 1;
  }
  if (!reassign_target &&
      (strcmp(label, "quota64") == 0 || strcmp(label, "quota128") == 0 ||
       strcmp(label, "quota256") == 0)) {
    struct fsxattr before = {0};
    struct fsxattr requested_attr;
    struct fsxattr after = {0};
    if (ioctl(fd, FS_IOC_FSGETXATTR, &before) != 0) {
      fprintf(stderr, "FS_IOC_FSGETXATTR before %s: %s\n", path,
              strerror(errno));
      close(fd);
      if (final_cleanup) cleanup_file(label, path);
      return 1;
    }
    requested_attr = before;
    requested_attr.fsx_projid = 0;
    errno = 0;
    int reassign_rc = ioctl(fd, FS_IOC_FSSETXATTR, &requested_attr);
    int reassign_errno = reassign_rc == -1 ? errno : 0;
    if (ioctl(fd, FS_IOC_FSGETXATTR, &after) != 0) {
      fprintf(stderr, "FS_IOC_FSGETXATTR after %s: %s\n", path,
              strerror(errno));
      close(fd);
      if (final_cleanup) cleanup_file(label, path);
      return 1;
    }
    printf("REASSIGN|%s|%d|%d|%u|%u\n", label, reassign_rc,
           reassign_errno, before.fsx_projid, after.fsx_projid);
  }
  const size_t chunk_size = 1024 * 1024;
  char *chunk = calloc(1, chunk_size);
  if (!chunk) {
    close(fd);
    if (final_cleanup) cleanup_file(label, path);
    return 1;
  }

  uint64_t written = 0;
  int write_errno = 0;
  int fsync_errno = 0;
  int fstat_errno = 0;
  int close_errno = 0;
  int unlinked = 0;
  while (written < requested) {
    size_t wanted = requested - written < chunk_size ? (size_t)(requested - written) : chunk_size;
    ssize_t n = write(fd, chunk, wanted);
    if (n < 0) {
      write_errno = errno;
      break;
    }
    written += (uint64_t)n;
    if (!unlinked && written >= unlink_after) {
      if (unlink(path) != 0) {
        write_errno = errno;
        break;
      }
      unlinked = 1;
    }
    if (fsync(fd) != 0) {
      fsync_errno = errno;
      break;
    }
    if ((size_t)n != wanted) {
      write_errno = EIO;
      break;
    }
  }
  struct stat st = {0};
  if (fstat(fd, &st) != 0) fstat_errno = errno;
  if (close(fd) != 0) close_errno = errno;
  printf("RESULT|%s|%" PRIu64 "|%" PRIu64 "|%d|%d|%d|%d|%u|%" PRIu64 "|%d\n",
         label, written, (uint64_t)st.st_blocks * 512, write_errno,
         fsync_errno, fstat_errno, close_errno, (unsigned)getuid(), cap_eff,
         unlinked);
  free(chunk);
  if (final_cleanup && cleanup_file(label, path) != 0) return 1;
  return 0;
}
