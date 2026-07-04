from __future__ import annotations

from dataclasses import dataclass
import platform
from typing import Any


@dataclass(frozen=True)
class ProviderPerformance:
    name: str
    label: str
    score: int
    tier: str
    notes: str


PROVIDER_PERFORMANCE: tuple[ProviderPerformance, ...] = (
    ProviderPerformance("NvTensorRTRTXExecutionProvider", "NVIDIA TensorRT RTX", 100, "GPU", "NVIDIA RTX TensorRT path"),
    ProviderPerformance("TensorrtExecutionProvider", "NVIDIA TensorRT", 98, "GPU", "NVIDIA TensorRT engine"),
    ProviderPerformance("CUDANHWCExecutionProvider", "NVIDIA CUDA NHWC", 94, "GPU", "NVIDIA CUDA NHWC layout"),
    ProviderPerformance("CudaPluginExecutionProvider", "NVIDIA CUDA plugin", 93, "GPU", "NVIDIA CUDA plugin"),
    ProviderPerformance("CUDAExecutionProvider", "NVIDIA CUDA", 92, "GPU", "NVIDIA CUDA kernels"),
    ProviderPerformance("MIGraphXExecutionProvider", "AMD MIGraphX", 88, "GPU", "AMD GPU acceleration"),
    ProviderPerformance("ROCMExecutionProvider", "AMD ROCm", 86, "GPU", "AMD ROCm acceleration"),
    ProviderPerformance("OpenVINOExecutionProvider", "Intel OpenVINO", 84, "Accelerator", "Intel CPU/GPU/NPU acceleration"),
    ProviderPerformance("QNNExecutionProvider", "Qualcomm QNN", 82, "NPU", "Qualcomm DSP/NPU acceleration"),
    ProviderPerformance("SNPEExecutionProvider", "Qualcomm SNPE", 80, "NPU", "Qualcomm Snapdragon acceleration"),
    ProviderPerformance("VitisAIExecutionProvider", "Xilinx Vitis AI", 78, "NPU", "Xilinx accelerator"),
    ProviderPerformance("CoreMLExecutionProvider", "Apple Core ML", 76, "Accelerator", "Apple Neural Engine/GPU/CPU"),
    ProviderPerformance("NnapiExecutionProvider", "Android NNAPI", 74, "Accelerator", "Android hardware acceleration API"),
    ProviderPerformance("VSINPUExecutionProvider", "VeriSilicon NPU", 72, "NPU", "VeriSilicon NPU acceleration"),
    ProviderPerformance("ACLExecutionProvider", "Arm Compute Library", 70, "CPU/GPU", "Arm CPU/GPU kernels"),
    ProviderPerformance("ArmNNExecutionProvider", "Arm NN", 68, "CPU/GPU/NPU", "Arm NN acceleration"),
    ProviderPerformance("DmlExecutionProvider", "DirectML", 66, "GPU", "Windows GPU acceleration"),
    ProviderPerformance("RknpuExecutionProvider", "Rockchip NPU", 64, "NPU", "Rockchip NPU acceleration"),
    ProviderPerformance("WebGpuExecutionProvider", "WebGPU", 62, "GPU", "Web GPU acceleration"),
    ProviderPerformance("WebNNExecutionProvider", "WebNN", 60, "Accelerator", "Web neural network API"),
    ProviderPerformance("XnnpackExecutionProvider", "XNNPACK", 58, "CPU", "Mobile CPU kernels"),
    ProviderPerformance("DnnlExecutionProvider", "oneDNN", 56, "CPU", "Intel oneDNN CPU kernels"),
    ProviderPerformance("TvmExecutionProvider", "TVM", 54, "Compiler", "TVM compiled execution"),
    ProviderPerformance("CANNExecutionProvider", "Huawei CANN", 52, "Accelerator", "Huawei Ascend acceleration"),
    ProviderPerformance("JsExecutionProvider", "JavaScript", 30, "Other", "JavaScript execution"),
    ProviderPerformance("AzureExecutionProvider", "Azure", 20, "Cloud", "Azure cloud execution"),
    ProviderPerformance("CPUExecutionProvider", "CPU", 10, "CPU", "Default ONNX Runtime CPU kernels"),
)

PROVIDER_BY_NAME = {provider.name: provider for provider in PROVIDER_PERFORMANCE}


def ranked_provider_names(providers: list[str]) -> list[str]:
    return [entry["name"] for entry in ranked_provider_info(providers)]


def ranked_provider_info(providers: list[str], available: set[str] | None = None) -> list[dict[str, Any]]:
    available_names = set(providers) if available is None else available
    return sorted(
        (provider_info(provider, provider in available_names) for provider in providers),
        key=lambda entry: (-int(entry["score"]), str(entry["name"])),
    )


def all_provider_rankings(available: list[str] | None = None) -> list[dict[str, Any]]:
    available_names = set(available or [])
    return ranked_provider_info([provider.name for provider in PROVIDER_PERFORMANCE], available_names)


def best_provider(providers: list[str]) -> str | None:
    ranked = ranked_provider_names(providers)
    if ranked:
        return ranked[0]
    return None


def provider_info(name: str, available: bool = False) -> dict[str, Any]:
    provider = PROVIDER_BY_NAME.get(name)
    if provider is None:
        return {
            "name": name,
            "label": name,
            "score": host_adjusted_score(name, 0),
            "tier": "Unknown",
            "notes": "Provider reported by ONNX Runtime",
            "available": available,
        }

    return {
        "name": provider.name,
        "label": provider.label,
        "score": host_adjusted_score(provider.name, provider.score),
        "tier": provider.tier,
        "notes": provider.notes,
        "available": available,
    }


def host_adjusted_score(name: str, score: int) -> int:
    machine = platform.machine().lower()
    is_arm = machine.startswith(("arm", "aarch64"))
    is_x86 = machine in {"x86_64", "amd64", "i386", "i686"}

    if name == "XnnpackExecutionProvider" and is_arm:
        return score + 10
    if name == "DnnlExecutionProvider" and is_arm:
        return score - 8
    if name == "DnnlExecutionProvider" and is_x86:
        return score + 8
    return score
