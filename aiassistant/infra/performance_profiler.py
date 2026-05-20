"""
Performance monitoring and profiling for mid-tier device optimization.

Tracks inference time, memory usage, and provides performance recommendations.
"""

import time
import threading
from typing import Dict, Optional
from dataclasses import dataclass, field
from datetime import datetime

try:
    import psutil
except ImportError:
    psutil = None


@dataclass
class PerformanceMetrics:
    """Track performance metrics for optimization analysis."""
    
    inference_times: list = field(default_factory=list)
    memory_peaks: list = field(default_factory=list)
    request_count: int = 0
    start_time: datetime = field(default_factory=datetime.now)
    last_inference_time: float = 0.0
    peak_memory_gb: float = 0.0
    
    def record_inference(self, duration_sec: float, memory_gb: float):
        """Record an inference operation."""
        self.inference_times.append(duration_sec)
        self.memory_peaks.append(memory_gb)
        self.request_count += 1
        self.last_inference_time = duration_sec
        
        if memory_gb > self.peak_memory_gb:
            self.peak_memory_gb = memory_gb
    
    def get_avg_inference_time(self) -> float:
        """Get average inference time."""
        if not self.inference_times:
            return 0.0
        return sum(self.inference_times) / len(self.inference_times)
    
    def get_p95_inference_time(self) -> float:
        """Get 95th percentile inference time."""
        if not self.inference_times:
            return 0.0
        sorted_times = sorted(self.inference_times)
        idx = max(0, int(len(sorted_times) * 0.95) - 1)
        return sorted_times[idx]
    
    def get_status_summary(self) -> str:
        """Get human-readable performance summary."""
        lines = [
            f"Requests: {self.request_count}",
            f"Avg latency: {self.get_avg_inference_time():.2f}s",
            f"P95 latency: {self.get_p95_inference_time():.2f}s",
            f"Peak memory: {self.peak_memory_gb:.2f}GB",
        ]
        return " | ".join(lines)


class PerformanceProfiler:
    """Profile inference performance for mid-tier optimization."""
    
    def __init__(self):
        self.metrics = PerformanceMetrics()
        self._lock = threading.Lock()
        self._inference_start_time = None
        self._inference_start_memory = None
    
    def start_inference(self) -> None:
        """Mark start of inference operation."""
        self._inference_start_time = time.time()
        if psutil:
            try:
                self._inference_start_memory = psutil.virtual_memory().used / (1024**3)
            except Exception:
                pass
    
    def end_inference(self) -> None:
        """Mark end of inference operation and record metrics."""
        if self._inference_start_time is None:
            return
        
        duration = time.time() - self._inference_start_time
        memory = 0.0
        
        if psutil and self._inference_start_memory is not None:
            try:
                current_memory = psutil.virtual_memory().used / (1024**3)
                memory = max(self._inference_start_memory, current_memory)
            except Exception:
                pass
        
        with self._lock:
            self.metrics.record_inference(duration, memory)
        
        self._inference_start_time = None
        self._inference_start_memory = None
    
    def get_metrics(self) -> PerformanceMetrics:
        """Get copy of current metrics."""
        with self._lock:
            return PerformanceMetrics(
                inference_times=self.metrics.inference_times.copy(),
                memory_peaks=self.metrics.memory_peaks.copy(),
                request_count=self.metrics.request_count,
                start_time=self.metrics.start_time,
                last_inference_time=self.metrics.last_inference_time,
                peak_memory_gb=self.metrics.peak_memory_gb,
            )
    
    def get_recommendations(self) -> list:
        """Get optimization recommendations based on metrics."""
        recommendations = []
        metrics = self.get_metrics()
        
        # Latency recommendations
        if metrics.get_avg_inference_time() > 6.0:
            recommendations.append(
                "⚠️ High latency (>6s): Consider using smaller model or lower context window"
            )
        elif metrics.get_avg_inference_time() > 4.0:
            recommendations.append(
                "📊 Acceptable latency (4-6s): Current optimization is suitable for mid-tier"
            )
        
        # Memory recommendations
        if metrics.peak_memory_gb > 7.0:
            recommendations.append(
                "⚠️ High memory (>7GB): Enable aggressive garbage collection or reduce context"
            )
        elif metrics.peak_memory_gb > 6.0:
            recommendations.append(
                "📊 Healthy memory (6-7GB): Monitor for peak usage patterns"
            )
        
        # Performance variance
        if metrics.request_count >= 5:
            p95 = metrics.get_p95_inference_time()
            avg = metrics.get_avg_inference_time()
            if p95 > avg * 1.5:
                recommendations.append(
                    "📈 High variance: Some requests slow; consider reducing model concurrency"
                )
        
        if not recommendations:
            recommendations.append("✅ Performance is optimal for your device class")
        
        return recommendations
    
    def print_status(self) -> None:
        """Print human-readable status."""
        metrics = self.get_metrics()
        print(f"\n📊 Performance Status: {metrics.get_status_summary()}", flush=True)
        
        for rec in self.get_recommendations():
            print(f"  {rec}", flush=True)
        print()


# Global profiler instance
_profiler = None


def get_profiler() -> PerformanceProfiler:
    """Get singleton profiler instance."""
    global _profiler
    if _profiler is None:
        _profiler = PerformanceProfiler()
    return _profiler


def profile_inference(func):
    """Decorator to profile inference function."""
    def wrapper(*args, **kwargs):
        profiler = get_profiler()
        profiler.start_inference()
        try:
            return func(*args, **kwargs)
        finally:
            profiler.end_inference()
    return wrapper


def print_profiler_status() -> None:
    """Print current profiler status."""
    get_profiler().print_status()
