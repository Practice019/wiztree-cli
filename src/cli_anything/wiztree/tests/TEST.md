# TEST.md — cli-anything-wiztree

Software: **WizTree 4.27** (`D:\software\WizTree\WizTree64.exe`, Antibody Software Limited)
Repository: `wiztree-cli` (standalone)

> Part 1 = 写代码**之前**定的测试计划。Part 2 = 跑完追加的真实结果。

---

## Part 0 — Phase 1 侦察结论（决定了下面所有测试的形态）

以下三条全部由**真机实测**得出，不是从文档抄的，也是本 harness 的测试重点：

| # | 实测结论 | 证据 | 对设计的强制约束 |
|---|---|---|---|
| **F1** | **退出码恒为 0**，包括失败情况 | `Start-Process ... -Wait -PassThru` → 非法路径 `Z:\nope` 返回 `ExitCode=0` 且**不产出任何文件** | 绝不能用 `returncode` 判成败，**必须校验产出文件存在且 size>0** |
| **F2** | **CSV 表头随系统语言本地化** | 中文系统实测首行 banner `生成由 WizTree 4.27 ... (您可以通过捐赠隐藏此信息)`，表头 `文件名称,大小,分配,修改时间,属性,文件,文件夹` | 解析器**必须按列位置**取值，禁止按列名/`csv.DictReader` 读取 |
| **F3** | **列数不稳定**，默认会在首条记录追加 `DRIVECAPACITY,FREESPACE,USEDSPACE,RESERVEDSPACE`（7 列 → 11 列） | 不带 `/exportdrivecapacity=0` 时 header 11 列；带上后稳定 7 列 | 必须**显式 pin 所有 advanced flag**，否则同一份代码对同一份数据解析结果不同 |

补充实测：`/export` 单次约 1.2s（20MB 目录）；`/exportfiletypes` 产出独立 schema（`扩展名,文件类型,百分比,大小,分配,文件`）；官方指南 + 二进制串双重核对确认参数表，网上流传的 `/drive:C:` `/maxdepth:` 是**错误**写法（真实为位置参数 + `/exportmaxdepth=n`）。

---

## Part 1 — 测试计划

### 1.1 单元测试 `tests/test_core.py`（纯合成数据，不依赖 WizTree 安装）

| 模块 | 被测函数 | 覆盖点 | 预计 |
|---|---|---|---|
| `core/index.py` | `parse_export_csv` | 中英文表头各一份；banner 行跳过；7 列 vs 11 列（带容量列）；字段含逗号与引号；ragged 行；空文件；只有 banner 没有数据；编码回退（utf-8-sig / gbk） | 12 |
| `core/index.py` | `WizTreeEntry.is_folder` | 结尾 `\` 判目录、结尾 `/` 判目录、普通文件 | 4 |
| `core/query.py` | `summary` | 总大小/文件数/文件夹数；空输入 | 4 |
| `core/query.py` | `top_entries` | 按 size 降序；`n` 截断；files_only / folders_only | 5 |
| `core/query.py` | `find_entries` | glob 通配、扩展名过滤、min/max size、limit、无命中 | 6 |
| `core/query.py` | `duplicates` | 同 size 同 name 聚合；不同 name 不聚合；`min_size` 阈值；空输入 | 5 |
| `core/query.py` | `ext_breakdown` | 扩展名归并、计数、降序 | 4 |
| `core/project.py` | `create/open/save/info/list_profiles` | 往返一致；不存在的工程报错；profiles 增删查 | 8 |
| `core/session.py` | `Session` | record/undo/redo/snapshot/history 上限；落盘再读回一致；损坏 JSON 不崩 | 8 |
| `core/export.py` | `render` | csv/json/table/markdown 四格式；top 截断；空输入 | 6 |
| `core/export.py` | `EXPORT_PRESETS` | 预设存在且含 filter/admin/sortby 键 | 2 |
| `core/export.py` | `verify_file` | 合法 CSV→valid；空文件→invalid；不存在→invalid；PNG magic；伪装成 csv 的二进制 | 6 |
| `utils/wiztree_backend.py` | `find_wiztree` | 显式路径命中；env `WIZTREE_EXE` 优先；找不到抛错且错误信息含下载地址 | 4 |
| `utils/wiztree_backend.py` | `build_export_args` | **F3 回归**：必须包含 pin schema 的 advanced flag；filter/exclude/admin/sortby/maxdepth 映射；`/exportmaxdepth` 而非 `/maxdepth` | 8 |
| `utils/wiztree_backend.py` | `run_export` | **F1 回归**：子进程 exit 0 但无产出文件 → 必须抛 `WizTreeExportError`，不得静默成功 | 3 |

合计约 **85** 条单元测试。

### 1.2 E2E 测试 `tests/test_full_e2e.py`（必须调真实 WizTree，无降级）

- **真实扫描 + 产物校验**：对 `D:\software\WizTree\locale`（约 1.2MB，实测 1.2s）真跑 `/export`，断言：
  - CSV 存在、size > 0；
  - `parse_export_csv` 解出条目数 > 0 且**目录条目数 ≥ 1**；
  - 解析出的根条目大小与 `os.scandir` 递归统计误差在 5% 内（交叉验证，不信任导出本身）；
  - 打印产物绝对路径与字节数，供人工开箱检查。
- **F1 回归（真实失败路径）**：对 `Z:\nope` 真跑，断言 CLI 返回非 0 且 stderr 有可读错误（而不是"成功"）。
- **F3 回归（真实 schema 稳定性）**：同一目标跑两次（一次走 CLI 默认路径），断言两次解析出的列名集合完全一致。
- **`/exportfiletypes`**：真实产出类型 CSV，断言行数 > 0 且百分比列可解析为 float。
- **treemap PNG**：真跑 `/treemapimagefile`，断言 PNG magic bytes + 尺寸在合理区间（> 1KB）。
- **Workflow 场景**（模拟真实运维任务）：*"找出 D 盘某目录下最大的 10 个文件、导出报告、再从缓存工程里复核"* —— 链式 `scan → query top → export report → project save/open → query again`，断言两次查询结果一致。
- **`TestCLISubprocess`**：用 `_resolve_cli("cli-anything-wiztree")`（不硬编码路径、不设 `cwd`、支持 `CLI_ANYTHING_FORCE_INSTALLED=1`）真实子进程调用 `--help` / `info --json` / `scan --json` / 失败的 scan（断言 returncode != 0）。

### 1.3 明确不做

- 不测 GUI 宏/点击：WizTree 有原生 CLI 导出接口，走 GUI 自动化是**降级方案**，只在原生接口缺失时才需要。
- 不测 `/admin=1` 提权路径：当前会话非管理员，真跑会弹 UAC；仅做参数构造的单元测试。

---

## Part 2 — 测试结果

执行命令（强制使用 PATH 上安装的 `cli-anything-wiztree`，而非开发回退）：

```powershell
cd wiztree-cli
$env:CLI_ANYTHING_FORCE_INSTALLED = '1'
python -m pytest cli_anything/wiztree/tests/ -v --tb=no
```

**结果：`============================ 154 passed in 47.42s =============================`**

> 未设 `CLI_ANYTHING_FORCE_INSTALLED` 时（走 `python -m` 回退）同样全绿，
> 所以「安装成命令」与「作为模块运行」两条路径都被覆盖。
>
> 另有一道独立质量门：`python validate_self.py` —— 按
> 上游 CLI-Anything 的 `validate.md` 验收清单，适配本项目布局后逐条检查，当前全部 PASS。

### 2.1 汇总统计

| 项 | 值 |
|---|---|
| 收集到的测试总数 | **154** |
| 单元测试（`test_core.py`） | 130 |
| E2E 测试（`test_full_e2e.py`） | 24 |
| 通过率 | 100% |
| 执行耗时 | 约 47 秒（其中真机 WizTree 扫描 20+ 次） |

Part 1 当时预估「约 85 条单元测试」，实际 **130** 条。多出来的是开发、自审与**两轮独立复核**
（事实核对 + 对抗复核）过程中为每条发现补的回归测试。

### 2.2 按类分布

| 测试类 | 条数 |
|---|---|
| `TestParseExportCsv` | 16 |
| `TestWizTreeEntry` | 4 |
| `TestQuery` | 25 |
| `TestProject` | 14 |
| `TestSession` | 9 |
| `TestExportRender` | 8 |
| `TestVerifyFile` | 9 |
| `TestParseSize` | 6 |
| `TestFindWizTree` | 15 |
| `TestBuildExportArgs` | 13 |
| `TestRunExportArtifactValidation` | 5 |
| `TestLauncherAndDiscoveryOrder` | 6 |
| `TestRealBackend` | 9 |
| `TestWorkflow` | 1 |
| `TestCLISubprocess` | 14 |

### 2.3 完整 `pytest -v --tb=no` 输出

```text
============================= test session starts =============================
platform win32 -- Python 3.13.5, pytest-8.4.1, pluggy-1.6.0 -- D:\software\anaconda3\python.exe
hypothesis profile 'default'
rootdir: D:\project_GIT\test\CLI-Anything\wiztree\agent-harness
plugins: anyio-4.12.1, hypothesis-6.152.6, langsmith-0.11.0, asyncio-1.1.0, cov-6.3.0, flask-1.3.0, mock-3.15.1, repeat-0.9.4, rerunfailures-15.1, timeout-2.4.0, xdist-3.8.0
asyncio: mode=Mode.STRICT, asyncio_default_fixture_loop_scope=None, asyncio_default_test_loop_scope=function
collecting ... collected 154 items

cli_anything/wiztree/tests/test_core.py::TestParseExportCsv::test_parses_chinese_header_positionally PASSED [  0%]
cli_anything/wiztree/tests/test_core.py::TestParseExportCsv::test_parses_english_header_positionally PASSED [  1%]
cli_anything/wiztree/tests/test_core.py::TestParseExportCsv::test_banner_line_is_skipped PASSED [  1%]
cli_anything/wiztree/tests/test_core.py::TestParseExportCsv::test_capacity_columns_do_not_shift_positional_fields PASSED [  2%]
cli_anything/wiztree/tests/test_core.py::TestParseExportCsv::test_quoted_name_containing_comma PASSED [  3%]
cli_anything/wiztree/tests/test_core.py::TestParseExportCsv::test_ragged_row_is_padded_not_crashed PASSED [  3%]
cli_anything/wiztree/tests/test_core.py::TestParseExportCsv::test_junk_rows_without_numeric_size_are_skipped PASSED [  4%]
cli_anything/wiztree/tests/test_core.py::TestParseExportCsv::test_empty_file_yields_no_entries PASSED [  5%]
cli_anything/wiztree/tests/test_core.py::TestParseExportCsv::test_banner_only_yields_no_entries PASSED [  5%]
cli_anything/wiztree/tests/test_core.py::TestParseExportCsv::test_encoding_fallback_to_cp936 PASSED [  6%]
cli_anything/wiztree/tests/test_core.py::TestParseExportCsv::test_missing_file_raises PASSED [  7%]
cli_anything/wiztree/tests/test_core.py::TestParseExportCsv::test_meta_reports_columns_and_encoding PASSED [  7%]
cli_anything/wiztree/tests/test_core.py::TestParseExportCsv::test_utf16_export_is_decoded_not_mojibake PASSED [  8%]
cli_anything/wiztree/tests/test_core.py::TestParseExportCsv::test_rows_with_unparseable_size_are_reported_not_silently_dropped PASSED [  9%]
cli_anything/wiztree/tests/test_core.py::TestParseExportCsv::test_clean_file_reports_no_skipped_rows PASSED [  9%]
cli_anything/wiztree/tests/test_core.py::TestParseExportCsv::test_banner_containing_commas_is_not_adopted_as_header PASSED [ 10%]
cli_anything/wiztree/tests/test_core.py::TestWizTreeEntry::test_backslash_suffix_is_folder PASSED [ 11%]
cli_anything/wiztree/tests/test_core.py::TestWizTreeEntry::test_forward_slash_suffix_is_folder PASSED [ 11%]
cli_anything/wiztree/tests/test_core.py::TestWizTreeEntry::test_plain_name_is_file PASSED [ 12%]
cli_anything/wiztree/tests/test_core.py::TestWizTreeEntry::test_extension_is_lowercased_and_dotless PASSED [ 12%]
cli_anything/wiztree/tests/test_core.py::TestQuery::test_summary_counts_and_sizes PASSED [ 13%]
cli_anything/wiztree/tests/test_core.py::TestQuery::test_summary_of_empty_input PASSED [ 14%]
cli_anything/wiztree/tests/test_core.py::TestQuery::test_summary_tolerates_none PASSED [ 14%]
cli_anything/wiztree/tests/test_core.py::TestQuery::test_summary_top_extension PASSED [ 15%]
cli_anything/wiztree/tests/test_core.py::TestQuery::test_top_entries_sorted_desc PASSED [ 16%]
cli_anything/wiztree/tests/test_core.py::TestQuery::test_top_entries_includes_folders_by_default PASSED [ 16%]
cli_anything/wiztree/tests/test_core.py::TestQuery::test_top_entries_limit PASSED [ 17%]
cli_anything/wiztree/tests/test_core.py::TestQuery::test_top_entries_files_only PASSED [ 18%]
cli_anything/wiztree/tests/test_core.py::TestQuery::test_top_entries_folders_only PASSED [ 18%]
cli_anything/wiztree/tests/test_core.py::TestQuery::test_top_entries_by_allocated PASSED [ 19%]
cli_anything/wiztree/tests/test_core.py::TestQuery::test_find_by_glob PASSED [ 20%]
cli_anything/wiztree/tests/test_core.py::TestQuery::test_find_by_extension_filter PASSED [ 20%]
cli_anything/wiztree/tests/test_core.py::TestQuery::test_find_by_min_size PASSED [ 21%]
cli_anything/wiztree/tests/test_core.py::TestQuery::test_find_by_max_size PASSED [ 22%]
cli_anything/wiztree/tests/test_core.py::TestQuery::test_find_limit PASSED [ 22%]
cli_anything/wiztree/tests/test_core.py::TestQuery::test_find_no_match PASSED [ 23%]
cli_anything/wiztree/tests/test_core.py::TestQuery::test_duplicates_groups_same_name_and_size PASSED [ 24%]
cli_anything/wiztree/tests/test_core.py::TestQuery::test_duplicates_respects_min_size PASSED [ 24%]
cli_anything/wiztree/tests/test_core.py::TestQuery::test_duplicates_ignores_folders PASSED [ 25%]
cli_anything/wiztree/tests/test_core.py::TestQuery::test_duplicates_empty_input PASSED [ 25%]
cli_anything/wiztree/tests/test_core.py::TestQuery::test_duplicates_reports_wasted_bytes PASSED [ 26%]
cli_anything/wiztree/tests/test_core.py::TestQuery::test_ext_breakdown_sorted_desc PASSED [ 27%]
cli_anything/wiztree/tests/test_core.py::TestQuery::test_ext_breakdown_skips_folders PASSED [ 27%]
cli_anything/wiztree/tests/test_core.py::TestQuery::test_ext_breakdown_limit PASSED [ 28%]
cli_anything/wiztree/tests/test_core.py::TestQuery::test_ext_breakdown_empty PASSED [ 29%]
cli_anything/wiztree/tests/test_core.py::TestProject::test_create_writes_file_and_reports_info PASSED [ 29%]
cli_anything/wiztree/tests/test_core.py::TestProject::test_roundtrip_preserves_fields PASSED [ 30%]
cli_anything/wiztree/tests/test_core.py::TestProject::test_open_missing_raises PASSED [ 31%]
cli_anything/wiztree/tests/test_core.py::TestProject::test_open_corrupt_raises_valueerror PASSED [ 31%]
cli_anything/wiztree/tests/test_core.py::TestProject::test_open_rejects_wrong_typed_profiles PASSED [ 32%]
cli_anything/wiztree/tests/test_core.py::TestProject::test_open_rejects_wrong_typed_stats PASSED [ 33%]
cli_anything/wiztree/tests/test_core.py::TestProject::test_open_rejects_missing_or_nonstring_root PASSED [ 33%]
cli_anything/wiztree/tests/test_core.py::TestProject::test_open_rejects_non_object_json PASSED [ 34%]
cli_anything/wiztree/tests/test_core.py::TestProject::test_open_repairs_null_profile_specs PASSED [ 35%]
cli_anything/wiztree/tests/test_core.py::TestProject::test_profiles_default_is_present PASSED [ 35%]
cli_anything/wiztree/tests/test_core.py::TestProject::test_profile_add_get_remove PASSED [ 36%]
cli_anything/wiztree/tests/test_core.py::TestProject::test_remove_unknown_profile_raises PASSED [ 37%]
cli_anything/wiztree/tests/test_core.py::TestProject::test_save_then_open_preserves_profiles PASSED [ 37%]
cli_anything/wiztree/tests/test_core.py::TestProject::test_info_includes_profile_list PASSED [ 38%]
cli_anything/wiztree/tests/test_core.py::TestSession::test_record_then_history PASSED [ 38%]
cli_anything/wiztree/tests/test_core.py::TestSession::test_undo_then_redo PASSED [ 39%]
cli_anything/wiztree/tests/test_core.py::TestSession::test_undo_on_empty_returns_none PASSED [ 40%]
cli_anything/wiztree/tests/test_core.py::TestSession::test_redo_on_empty_returns_none PASSED [ 40%]
cli_anything/wiztree/tests/test_core.py::TestSession::test_record_clears_redo_stack PASSED [ 41%]
cli_anything/wiztree/tests/test_core.py::TestSession::test_history_limit PASSED [ 42%]
cli_anything/wiztree/tests/test_core.py::TestSession::test_snapshot_and_restore PASSED [ 42%]
cli_anything/wiztree/tests/test_core.py::TestSession::test_persist_roundtrip PASSED [ 43%]
cli_anything/wiztree/tests/test_core.py::TestSession::test_corrupt_session_file_does_not_crash PASSED [ 44%]
cli_anything/wiztree/tests/test_core.py::TestExportRender::test_render_csv_has_header_and_rows PASSED [ 44%]
cli_anything/wiztree/tests/test_core.py::TestExportRender::test_render_json_is_parseable PASSED [ 45%]
cli_anything/wiztree/tests/test_core.py::TestExportRender::test_render_json_respects_top PASSED [ 46%]
cli_anything/wiztree/tests/test_core.py::TestExportRender::test_render_table_is_aligned_text PASSED [ 46%]
cli_anything/wiztree/tests/test_core.py::TestExportRender::test_render_markdown_has_pipes PASSED [ 47%]
cli_anything/wiztree/tests/test_core.py::TestExportRender::test_render_empty_input_does_not_crash PASSED [ 48%]
cli_anything/wiztree/tests/test_core.py::TestExportRender::test_export_presets_contain_required_keys PASSED [ 48%]
cli_anything/wiztree/tests/test_core.py::TestExportRender::test_export_presets_include_cleanup_scenario PASSED [ 49%]
cli_anything/wiztree/tests/test_core.py::TestVerifyFile::test_valid_csv PASSED [ 50%]
cli_anything/wiztree/tests/test_core.py::TestVerifyFile::test_empty_file_is_invalid PASSED [ 50%]
cli_anything/wiztree/tests/test_core.py::TestVerifyFile::test_missing_file_is_invalid PASSED [ 51%]
cli_anything/wiztree/tests/test_core.py::TestVerifyFile::test_png_magic_bytes PASSED [ 51%]
cli_anything/wiztree/tests/test_core.py::TestVerifyFile::test_binary_masquerading_as_csv_is_invalid PASSED [ 52%]
cli_anything/wiztree/tests/test_core.py::TestVerifyFile::test_mft_dump_detected PASSED [ 53%]
cli_anything/wiztree/tests/test_core.py::TestVerifyFile::test_banner_and_header_only_is_not_a_valid_csv PASSED [ 53%]
cli_anything/wiztree/tests/test_core.py::TestVerifyFile::test_csv_with_one_data_row_is_valid PASSED [ 54%]
cli_anything/wiztree/tests/test_core.py::TestVerifyFile::test_utf16_csv_is_not_judged_binary PASSED [ 55%]
cli_anything/wiztree/tests/test_core.py::TestParseSize::test_plain_and_suffixed PASSED [ 55%]
cli_anything/wiztree/tests/test_core.py::TestParseSize::test_empty_is_none PASSED [ 56%]
cli_anything/wiztree/tests/test_core.py::TestParseSize::test_non_finite_is_a_value_error_not_an_overflow PASSED [ 57%]
cli_anything/wiztree/tests/test_core.py::TestParseSize::test_junk_is_a_value_error PASSED [ 57%]
cli_anything/wiztree/tests/test_core.py::TestParseSize::test_absurdly_large_is_rejected PASSED [ 58%]
cli_anything/wiztree/tests/test_core.py::TestParseSize::test_large_integer_keeps_full_precision PASSED [ 59%]
cli_anything/wiztree/tests/test_core.py::TestFindWizTree::test_explicit_path_wins PASSED [ 59%]
cli_anything/wiztree/tests/test_core.py::TestFindWizTree::test_env_var_is_used PASSED [ 60%]
cli_anything/wiztree/tests/test_core.py::TestFindWizTree::test_env_var_beats_registry PASSED [ 61%]
cli_anything/wiztree/tests/test_core.py::TestFindWizTree::test_registry_discovery_works PASSED [ 61%]
cli_anything/wiztree/tests/test_core.py::TestFindWizTree::test_environment_directory_discovery PASSED [ 62%]
cli_anything/wiztree/tests/test_core.py::TestFindWizTree::test_remembered_config_is_consulted PASSED [ 62%]
cli_anything/wiztree/tests/test_core.py::TestFindWizTree::test_search_paths_from_config_are_honoured PASSED [ 63%]
cli_anything/wiztree/tests/test_core.py::TestFindWizTree::test_discovery_does_not_write_config PASSED [ 64%]
cli_anything/wiztree/tests/test_core.py::TestFindWizTree::test_remember_exe_persists_and_is_idempotent PASSED [ 64%]
cli_anything/wiztree/tests/test_core.py::TestFindWizTree::test_stale_remembered_exe_falls_through_to_registry PASSED [ 65%]
cli_anything/wiztree/tests/test_core.py::TestFindWizTree::test_corrupt_config_is_tolerated PASSED [ 66%]
cli_anything/wiztree/tests/test_core.py::TestFindWizTree::test_missing_raises_with_download_hint PASSED [ 66%]
cli_anything/wiztree/tests/test_core.py::TestFindWizTree::test_explicit_nonexistent_path_raises PASSED [ 67%]
cli_anything/wiztree/tests/test_core.py::TestFindWizTree::test_probe_reports_source_and_never_raises PASSED [ 68%]
cli_anything/wiztree/tests/test_core.py::TestFindWizTree::test_probe_reports_found_source PASSED [ 68%]
cli_anything/wiztree/tests/test_core.py::TestBuildExportArgs::test_first_arg_is_target_and_export_present PASSED [ 69%]
cli_anything/wiztree/tests/test_core.py::TestBuildExportArgs::test_pins_schema_flags_by_default PASSED [ 70%]
cli_anything/wiztree/tests/test_core.py::TestBuildExportArgs::test_filter_and_exclude PASSED [ 70%]
cli_anything/wiztree/tests/test_core.py::TestBuildExportArgs::test_admin_flag_override PASSED [ 71%]
cli_anything/wiztree/tests/test_core.py::TestBuildExportArgs::test_sortby_mapping PASSED [ 72%]
cli_anything/wiztree/tests/test_core.py::TestBuildExportArgs::test_max_depth_uses_exportmaxdepth_not_maxdepth PASSED [ 72%]
cli_anything/wiztree/tests/test_core.py::TestBuildExportArgs::test_folder_and_file_toggles PASSED [ 73%]
cli_anything/wiztree/tests/test_core.py::TestBuildExportArgs::test_invalid_sortby_rejected PASSED [ 74%]
cli_anything/wiztree/tests/test_core.py::TestBuildExportArgs::test_treemap_defaults_match_the_vendor PASSED [ 74%]
cli_anything/wiztree/tests/test_core.py::TestBuildExportArgs::test_treemap_explicit_size_and_flags PASSED [ 75%]
cli_anything/wiztree/tests/test_core.py::TestBuildExportArgs::test_treemap_rejects_nonpositive_size PASSED [ 75%]
cli_anything/wiztree/tests/test_core.py::TestBuildExportArgs::test_mft_args_shape PASSED [ 76%]
cli_anything/wiztree/tests/test_core.py::TestBuildExportArgs::test_probe_never_raises_when_the_exe_vanishes PASSED [ 77%]
cli_anything/wiztree/tests/test_core.py::TestRunExportArtifactValidation::test_exit_zero_without_artifact_raises PASSED [ 77%]
cli_anything/wiztree/tests/test_core.py::TestRunExportArtifactValidation::test_artifact_produced_reports_success PASSED [ 78%]
cli_anything/wiztree/tests/test_core.py::TestRunExportArtifactValidation::test_zero_byte_artifact_is_rejected PASSED [ 79%]
cli_anything/wiztree/tests/test_core.py::TestRunExportArtifactValidation::test_artifact_appearing_after_the_process_returns_is_accepted PASSED [ 79%]
cli_anything/wiztree/tests/test_core.py::TestRunExportArtifactValidation::test_unwritable_destination_is_refused_before_launching PASSED [ 80%]
cli_anything/wiztree/tests/test_core.py::TestLauncherAndDiscoveryOrder::test_is_launcher_only PASSED [ 81%]
cli_anything/wiztree/tests/test_core.py::TestLauncherAndDiscoveryOrder::test_64_bit_is_ordered_first PASSED [ 81%]
cli_anything/wiztree/tests/test_core.py::TestLauncherAndDiscoveryOrder::test_64_bit_sibling_is_inserted_for_a_launcher PASSED [ 82%]
cli_anything/wiztree/tests/test_core.py::TestLauncherAndDiscoveryOrder::test_iter_candidates_offers_64_bit_before_32_bit PASSED [ 83%]
cli_anything/wiztree/tests/test_core.py::TestLauncherAndDiscoveryOrder::test_env_candidates_are_rooted_not_drive_relative PASSED [ 83%]
cli_anything/wiztree/tests/test_core.py::TestLauncherAndDiscoveryOrder::test_env_candidates_are_deduplicated PASSED [ 84%]
cli_anything/wiztree/tests/test_full_e2e.py::TestRealBackend::test_wiztree_is_installed PASSED [ 85%]
cli_anything/wiztree/tests/test_full_e2e.py::TestRealBackend::test_export_produces_verifiable_csv PASSED [ 85%]
cli_anything/wiztree/tests/test_full_e2e.py::TestRealBackend::test_parsed_entries_have_folders_and_files PASSED [ 86%]
cli_anything/wiztree/tests/test_full_e2e.py::TestRealBackend::test_parsed_total_matches_filesystem PASSED [ 87%]
cli_anything/wiztree/tests/test_full_e2e.py::TestRealBackend::test_f1_real_failure_is_reported_not_silently_successful PASSED [ 87%]
cli_anything/wiztree/tests/test_full_e2e.py::TestRealBackend::test_f3_columns_are_identical_between_runs PASSED [ 88%]
cli_anything/wiztree/tests/test_full_e2e.py::TestRealBackend::test_filter_restricts_export_to_matching_files PASSED [ 88%]
cli_anything/wiztree/tests/test_full_e2e.py::TestRealBackend::test_filetypes_report_is_parsable PASSED [ 89%]
cli_anything/wiztree/tests/test_full_e2e.py::TestRealBackend::test_treemap_png_export PASSED [ 90%]
cli_anything/wiztree/tests/test_full_e2e.py::TestWorkflow::test_scan_query_report_reopen PASSED [ 90%]
cli_anything/wiztree/tests/test_full_e2e.py::TestCLISubprocess::test_help PASSED [ 91%]
cli_anything/wiztree/tests/test_full_e2e.py::TestCLISubprocess::test_info_json PASSED [ 92%]
cli_anything/wiztree/tests/test_full_e2e.py::TestCLISubprocess::test_scan_run_json PASSED [ 92%]
cli_anything/wiztree/tests/test_full_e2e.py::TestCLISubprocess::test_query_reads_state_from_a_previous_invocation PASSED [ 93%]
cli_anything/wiztree/tests/test_full_e2e.py::TestCLISubprocess::test_failing_scan_exits_nonzero PASSED [ 94%]
cli_anything/wiztree/tests/test_full_e2e.py::TestCLISubprocess::test_export_report_and_session PASSED [ 94%]
cli_anything/wiztree/tests/test_full_e2e.py::TestCLISubprocess::test_help_of_nested_groups PASSED [ 95%]
cli_anything/wiztree/tests/test_full_e2e.py::TestCLISubprocess::test_global_flag_works_after_the_subcommand PASSED [ 96%]
cli_anything/wiztree/tests/test_full_e2e.py::TestCLISubprocess::test_global_flag_after_subcommand_on_nested_groups PASSED [ 96%]
cli_anything/wiztree/tests/test_full_e2e.py::TestCLISubprocess::test_admin_flag_after_subcommand_is_accepted PASSED [ 97%]
cli_anything/wiztree/tests/test_full_e2e.py::TestCLISubprocess::test_wildcard_argument_is_not_glob_expanded PASSED [ 98%]
cli_anything/wiztree/tests/test_full_e2e.py::TestCLISubprocess::test_usage_error_still_emits_json PASSED [ 98%]
cli_anything/wiztree/tests/test_full_e2e.py::TestCLISubprocess::test_empty_export_is_an_error_unless_allowed PASSED [ 99%]
cli_anything/wiztree/tests/test_full_e2e.py::TestCLISubprocess::test_unwritable_destination_fails_fast PASSED [100%]

============================ 154 passed in 47.42s =============================
```

### 2.4 覆盖说明与已知缺口

**被真机验证过的（不是 mock）**

- F1：`Z:\definitely-not-a-drive` 真跑 → WizTree 退出码 0 且不产出文件 → 后端抛
  `WizTreeExportError`，CLI 退出码 1 并返回可读 JSON 错误。
- F2：中文系统产出的 CSV（`文件名称,大小,分配,…` 表头 + 中文 banner）被正确解析。
- F3：同一目标连续两次导出，列名集合完全一致，且恒为 7 列（capacity 列被 pin 掉）。
- F4：`--exe WizTree.exe`（32 位启动器）真跑成功，产出 114 条，`exe_is_launcher_only=true`
  —— 修复前这是 3/3 次的假失败。
- F5：`-o C:\Windows\…` 真跑 **0 秒**返回清晰错误（修复前是 30 分钟静默挂起）。
- 交叉验证：解析出的根目录大小与独立 `os.walk` 统计一致（容差 5%）。
- `/exportfiletypes` 真实产出可解析的类型报告（百分比列为 float）。
- treemap 真实产出 PNG，默认尺寸按 PNG IHDR 校验为 1920×1080。
- 全局选项两种位置都可用：`--json info` 与 `info --json` 结果一致。
- 通配符参数不再被 Click 展开（在含 2 个 `*.mp4` 文件的目录里真跑验证）。

**明确未覆盖（本套测试）**

- `/admin=1` 提权路径：当前会话非管理员，真跑会弹 UAC。仅做参数构造的单元测试。
- `scan mft`（`/dumpmftfile`）：本套测试只有 magic-byte 单元测试覆盖。
  > 独立核对时**真跑过**：**不需要提权**。非管理员执行返回退出码 0 并产出
  > 5,675,941,993 字节（约 5.3 GB）的合法 dump，首部 `46 49 4C 45 30` = `FILE0`。
  > 提权是 `/exportmftrecno` 的要求，与 `/dumpmftfile` 无关。该 dump 采样后已删除。
- macOS（WizTreeMac 用 `--paramname`）：本 harness 只面向 Windows。
- WizTree 的 GUI-only 搜索语言（日期、正则、`namelen`）：见 `WIZTREE.md` §3 缺口评估。
- 并发：`session.json` 走独占文件锁（`msvcrt` on Windows / `fcntl` on POSIX），但没有
  多进程并发写入的压力测试。
- 硬链接的前导 `0` 标记：测试卷上没有硬链接，未能复现；`query dups` 因此没有对硬链接做
  排除（对抗复核实测真实扫描 `group_count=0`，未观察到膨胀）。
- `/exportfiletypes` 的 schema 只有真机解析（非空 + 百分比为 float），没有逐列断言。

### 2.5 第一轮：开发与自审发现并修复的缺陷（1–6）

| # | 缺陷 | 被发现的方式 | 修法 |
|---|---|---|---|
| 1 | `_to_int` 不接受 int 输入，`WizTreeEntry(size=1)` 抛 `AttributeError` | 单元测试构造条目 | `_to_int` 接受 int/float/bool |
| 2 | CLI 中 `query` / `index` 模块名被同名 Click 组遮蔽，`query.summary` 解析到 `Group` 对象 | 真机冒烟（首次 `scan run` 崩） | 模块改别名 `query_mod` / `index_mod` |
| 3 | 失败信息把 **exe 路径**当成扫描目标打印 | 真机跑 `Z:\nope` 读错误文本 | 增加 `subject` 参数 |
| 4 | `discover()` 把发现的路径静默写进用户配置；单元测试注入的临时假 exe **污染了开发机真实配置**，导致后续所有 E2E 指向不存在的路径 | 全量跑测试 7 failed / 4 errors | ①`discover()` 改纯函数，持久化改为 CLI `info` 显式调用 `remember_exe()`；②两个测试模块加 autouse 隔离 fixture；③补 3 条回归测试 |
| 5 | 布尔值被当整数格式化（`found: 1`） | 人工读 `info` 输出 | `_print_human` 先判 bool |
| 6 | `DEFAULT_SEARCH_PATHS` 硬编码本机安装路径，违反验收清单 §8 | 自审 61 项检查 | 改为空元组；新增注册表 `InstallLocation` 发现 + 环境变量派生目录 + 用户 `search_paths`；测试目标也改为从发现结果派生 |

### 2.6 第二轮：两轮独立复核发现并修复的缺陷（7–11 事实核对；12–24 对抗复核）

**事实核对（对照厂商指南 + 二进制）**

| # | 缺陷 | 修法 |
|---|---|---|
| 7 | **全局选项只能写在动词前**：`info --json` → exit 2，而 README 有 4 处正是这么写的 | 新增 `with_globals` 装饰器，两种顺序等价；补 3 条子进程回归测试。**选择改代码而不是改文档** |
| 8 | treemap 默认 1024×768，厂商默认 1920×1080 | 默认改为 1920×1080；补 2 条参数构造测试 |
| 9 | 文档声称 `/dumpmftfile` 需要提权 —— 实测非管理员即可 | 更正 README / WIZTREE.md / 本文档 §2.4 |
| 10 | `probe()` 承诺「从不抛异常」，却在 `getsize()` 上抛 `FileNotFoundError` | try/except OSError 包住 found 分支；补 1 条回归测试 |
| 11 | 文档不精确：属性掩码漏了 `16`（目录）；「所有字段都带引号」不对 | 更正 `WIZTREE.md` §1.3 |

**对抗复核（16 条，按严重度）**

| # | 严重度 | 缺陷 | 修法 |
|---|---|---|---|
| 12 | **HIGH** | **挂起**：`-o C:\Windows\…` 时 WizTree 弹出模态对话框（无人可点）而永不返回；`--timeout` 默认 1800s + DEVNULL → **30 分钟静默** | `_preflight_output()` 在启动前探测目标目录可写性 → 立即报错。真机验证：30 分钟 → **0 秒** |
| 13 | **HIGH** | **假失败**：32 位 `WizTree.exe` 只负责拉起 64 位进程就返回，产物 ~2s 后才出现 → 3/3 次报「no output」 | ①发现顺序优先 64 位并补上同目录的 64 位兄弟；②进程返回后按 grace 等待产物出现（启动器给更长时间）。真机验证：成功 114 条 |
| 14 | **HIGH** | **通配符被展开**：Click 8 在 Windows 上默认展开 `sys.argv`，`query find "*.mp4"` 被替换成当前目录的 .mp4 文件（≥2 个匹配 → rc=2；1 个 → 静默改模式） | `cli.main(windows_expand_args=False)`。这是本轮最有价值的发现 —— 测试全绿是因为**没有一条测试把通配符从控制台脚本传进去**，已补上这条测试 |
| 15 | **MED** | `{"profiles":"oops"}` → `AttributeError` traceback 且 `--json` 下 stdout 为空 | `Project.open()` 按**类型**校验 schema；同时把 `AttributeError/TypeError/OverflowError` 纳入统一错误处理 |
| 16 | **MED** | `--min-size inf`（及 `1e400`）→ `OverflowError` traceback，而 `abc` 是干净报错 —— 契约不一致 | `parse_size` 拒绝非有限值；纯整数走 int 路径保留精度 |
| 17 | **MED** | 空/非数字 `size` 字段**静默丢行**，而 `verify_file` 仍报 valid → 总和被低估 | 解析器报告 `skipped_rows`，CLI 作为 warning 输出；补 1 条测试 |
| 18 | **MED** | UTF-16 输入 → latin-1 乱码 0 条且无错误（latin-1 永不失败） | `_decode` 增加 UTF-16（先于 latin-1）并返回 `encoding_fallback` 标记；补 2 条测试 |
| 19 | **MED** | `verify_file` 把「banner+表头」的 150 字节文件认证为合法 CSV → `scan run --filter "*.zzzzz"` 退出 0 并打印 `✓ Scanned` | `verify_file` 要求 ≥1 条数据行并报告 `data_rows`；`scan run` 遇 0 条默认**报错**，新增 `--allow-empty`；补 4 条测试 |
| 20 | **MED** | `_env_candidates()` 由 `SystemDrive=C:` 拼出**驱动器相对路径** `C:WizTree\…`，会相对于 C: 当前目录解析 | 裸盘符补 `os.sep`；顺带去重（ProgramFiles == ProgramW6432）；补 2 条测试 |
| 21 | LOW | banner 含 ≥5 个逗号字段时被当成表头 → `meta["columns"]` 变垃圾 | `_split_banner` 收紧判定（短行 + 含 WizTree 且第二列非数字）；补 1 条测试 |
| 22 | LOW | `query dups` 忽略硬链接标记，可能虚报可回收空间 | 文档记录该限制（真实扫描未观察到，见 §2.4） |
| 23 | LOW | `--json` 下 Click 用法错误返回**空 stdout** | `main()` 改 `standalone_mode=False` 并统一走 JSON 错误通道；补 1 条测试 |
| 24 | LOW | `sys.path.insert` 深了一层，对 `import cli_anything.wiztree` 无效 | 修正为 harness 根目录 |

> 第 12/13/14 条都不是靠「多写测试」发现的，而是靠**让一个独立视角去主动证伪**。
> 尤其第 14 条：测试覆盖再高，也覆盖不到「测试自己没走过的那条参数通道」。
> 三条现已各有专门的回归测试，且都在真机上验证过修复前后的差异。
