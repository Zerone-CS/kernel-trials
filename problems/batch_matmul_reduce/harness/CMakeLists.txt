cmake_minimum_required(VERSION 3.16)

# Retain the official project's ASC discovery, architecture option and libraries.
find_package(ASC REQUIRED)

project(op_eval_runner LANGUAGES ASC CXX)
set(CMAKE_CXX_STANDARD 14)

if(DEFINED NPU_ARCH)
    set(SOC_ARCH ${NPU_ARCH})
else()
    set(SOC_ARCH "dav-2201")
endif()

add_executable(op_eval_runner main.asc)
target_link_libraries(op_eval_runner PRIVATE
    tiling_api
    register
    platform
    unified_dlog
    dl
    m
    graph_base
)
target_include_directories(op_eval_runner PRIVATE ${CMAKE_CURRENT_SOURCE_DIR})
target_compile_options(op_eval_runner PRIVATE
    $<$<COMPILE_LANGUAGE:ASC>:--npu-arch=${SOC_ARCH}>
)
