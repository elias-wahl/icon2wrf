import sys
import time

class ProgressTracker:
    def __init__(self, queue):
        self.queue = queue
        self.total_files = 0
        self.completed = 0
        self.download_times = []
        self.download_sizes = []
        self.process_times = []
        self.start_time = None
        
    def _format_time(self, seconds):
        if seconds < 0:
            return "00:00:00"
        h = int(seconds // 3600)
        m = int((seconds % 3600) // 60)
        s = int(seconds % 60)
        if h > 0:
            return f"{h}h {m}m {s}s"
        return f"{m}m {s}s"
        
    def _render(self):
        # Go up 5 lines and clear screen downwards
        sys.stdout.write("\033[5F\033[J")
        
        # Calculate stats
        pct = (self.completed / self.total_files * 100) if self.total_files > 0 else 0
        avg_dl = sum(self.download_times) / len(self.download_times) if self.download_times else 0.0
        avg_pr = sum(self.process_times) / len(self.process_times) if self.process_times else 0.0
        
        speed = avg_dl + avg_pr
        elapsed = time.time() - self.start_time if self.start_time else 0
        
        # Global ETA based on parallel throughput
        remaining = self.total_files - self.completed
        global_speed = elapsed / self.completed if self.completed > 0 else 0.0
        eta = remaining * global_speed
        
        avg_dl_size_mb = (sum(self.download_sizes) / len(self.download_sizes)) / (1024*1024) if self.download_sizes else 0.0
        dl_speed_mb_s = (avg_dl_size_mb / avg_dl) if avg_dl > 0 else 0.0
        
        if avg_dl > avg_pr * 1.5:
            bottleneck = f"NETWORK ({avg_dl/(speed)*100:.0f}%)" if speed > 0 else "NETWORK"
        elif avg_pr > avg_dl * 1.5:
            bottleneck = f"CPU ({avg_pr/(speed)*100:.0f}%)" if speed > 0 else "CPU"
        else:
            bottleneck = "BALANCED"
            
        bar_len = 35
        filled = int(bar_len * (pct / 100)) if self.total_files > 0 else 0
        bar = "#" * filled + "." * (bar_len - filled)
        
        sys.stdout.write("-" * 75 + "\n")
        sys.stdout.write(f"[{bar}] {pct:.1f}% ({self.completed} / {self.total_files} files globally)\n")
        sys.stdout.write(f"⚡ Global Throughput: {global_speed:.2f}s/file | ⏳ Global ETA: {self._format_time(eta)} | ⏱  Elapsed: {self._format_time(elapsed)}\n")
        sys.stdout.write(f"🔍 Worker Avg: DL {avg_dl:.1f}s, Proc {avg_pr:.1f}s | 📊 Bottleneck: {bottleneck} | 🌐 DL: {dl_speed_mb_s:.2f} MB/s\n")
        sys.stdout.write("-" * 75 + "\n")
        sys.stdout.flush()

    def monitor(self):
        # Print initial 5 newlines so we have room to render the bar at the bottom
        sys.stdout.write("\n\n\n\n\n")
        sys.stdout.flush()
        
        while True:
            msg = self.queue.get()
            if msg.get("type") == "STOP":
                # Final render
                self._render()
                break
                
            mtype = msg.get("type")
            worker_id = msg.get("worker_id", "?")
            
            if mtype == "LOG":
                sys.stdout.write("\033[5F\033[J")
                sys.stdout.write(f"[Worker {worker_id}] {msg.get('msg')}\n")
                sys.stdout.write("\n\n\n\n\n")
                
            elif mtype == "INIT_TOTAL":
                self.total_files += msg.get("total", 0)
                if self.start_time is None:
                    self.start_time = time.time()
                    
            elif mtype == "PROCESS_START":
                dl_time = msg.get("duration")
                fsize = msg.get("file_size", 0)
                if dl_time is not None:
                    self.download_times.append(dl_time)
                    self.download_sizes.append(fsize)
                    if len(self.download_times) > 100:
                        self.download_times.pop(0)
                        self.download_sizes.pop(0)
                        
            elif mtype == "STEP_COMPLETE":
                pr_time = msg.get("duration")
                if pr_time is not None:
                    self.process_times.append(pr_time)
                    if len(self.process_times) > 100:
                        self.process_times.pop(0)
                self.completed += 1
                
            self._render()
