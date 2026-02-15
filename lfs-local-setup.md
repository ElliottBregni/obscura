# Local Git LFS Storage Setup ✅

Successfully tested local LFS storage with the obscura repo!

## What We Did

1. **Installed Git LFS**
   ```bash
   brew install git-lfs
   git lfs install
   ```

2. **Created Local LFS Storage**
   ```bash
   mkdir -p ~/dev/.lfs-storage/obscura
   cd ~/dev/.lfs-storage/obscura
   git init --bare
   ```

3. **Configured Repo to Use Local LFS**
   ```bash
   cd ~/dev/obscura-main
   git config -f .lfsconfig lfs.url file:///Users/elliottbregni/dev/.lfs-storage/obscura
   git add .lfsconfig
   git commit -m "local LFS file config"
   ```

4. **Tested with Binary File**
   ```bash
   git lfs track "*.bin"
   # Created 100KB test file
   git add .gitattributes test-lfs.bin
   git commit -m "test LFS"
   ```

5. **Verified in New Worktree**
   ```bash
   git worktree add -b test-lfs ../obscura-test-lfs main
   cd ../obscura-test-lfs
   git lfs pull  # Successfully retrieved from local storage
   ```

## How It Works

- **LFS Pointer in Git**: Git stores a tiny pointer file (~130 bytes) instead of the full binary
- **Local Storage**: Actual files are stored in `~/dev/.lfs-storage/obscura/`
- **Worktrees Share LFS**: All worktrees use the same local LFS storage
- **No Network Dependency**: Files are stored and retrieved locally

## File Locations

```
~/dev/
  .lfs-storage/
    obscura/           # Bare repo storing LFS objects
  obscura/             # Main bare git repo
  obscura-main/        # Worktree with .lfsconfig pointing to local storage
```

## Benefits

1. ✅ **No GitHub LFS Bandwidth/Storage Limits**: Free local storage
2. ✅ **Faster**: No network latency
3. ✅ **Offline Work**: No internet needed
4. ✅ **Multiple Worktrees**: All share the same LFS cache
5. ✅ **Portable**: Easy to backup with the repo

## To Use in Other Repos

```bash
# 1. Create LFS storage
mkdir -p ~/dev/.lfs-storage/REPO_NAME
cd ~/dev/.lfs-storage/REPO_NAME
git init --bare

# 2. Configure repo
cd ~/dev/REPO_NAME-main
git config -f .lfsconfig lfs.url file:///Users/elliottbregni/dev/.lfs-storage/REPO_NAME
git add .lfsconfig
git commit -m "local LFS config"

# 3. Track file types
git lfs track "*.psd"
git lfs track "*.bin"
git lfs track "*.zip"
git add .gitattributes
git commit -m "track large files with LFS"
```

## Verification Commands

```bash
# Check LFS config
git lfs env | grep Endpoint

# List LFS files
git lfs ls-files

# Check what's tracked
cat .gitattributes

# See LFS storage size
du -sh ~/dev/.lfs-storage/REPO_NAME
```

## Test Results

- ✅ 100KB binary file tracked correctly
- ✅ Git stored pointer (not full file)
- ✅ New worktree retrieved file from local storage
- ✅ MD5 checksums matched across worktrees
- ✅ Local storage only 80KB total
