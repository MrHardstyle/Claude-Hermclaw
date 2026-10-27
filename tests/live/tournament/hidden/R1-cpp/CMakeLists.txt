# Hidden checks of ladder task R1-cpp (roman numerals): builds the delivered src/roemisch.cpp with include/roemisch.h
# against these GoogleTest checks, outside the delivered build files.
cmake_minimum_required(VERSION 3.20)
project(hidden_roemisch CXX)
set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
find_package(GTest REQUIRED)
set(ROOT ${CMAKE_CURRENT_SOURCE_DIR}/..)
add_executable(hidden_tests hidden_test.cpp ${ROOT}/src/roemisch.cpp)
target_include_directories(hidden_tests PRIVATE ${ROOT}/include)
target_link_libraries(hidden_tests GTest::gtest_main)
