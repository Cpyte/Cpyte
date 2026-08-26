; ModuleID = "main"
target triple = "arm64-apple-darwin25.5.0"
target datalayout = "e-m:o-p270:32:32-p271:32:32-p272:64:64-i64:64-i128:128-n32:64-S128-Fn32"

declare void @"print_int"(i32 %".1")

declare void @"print_int64"(i64 %".1")

declare void @"print_uint64"(i64 %".1")

declare void @"print_double"(double %".1")

declare void @"print_hex"(i64 %".1")

declare void @"print_str"(i8* %".1")

declare i8* @"str_of_int64"(i64 %".1")

declare i8* @"str_of_uint64"(i64 %".1")

declare i8* @"str_of_ptr"(i64 %".1")

declare i8* @"str_of_double"(double %".1")

declare i32 @"input_int"()

declare i8* @"input_str"()

declare i8* @"bigint_new"()

declare void @"bigint_free"(i8* %".1")

declare i8* @"bigint_from_int"(i64 %".1")

declare i8* @"bigint_from_uint64"(i64 %".1")

declare i8* @"bigint_from_str"(i8* %".1")

declare i8* @"bigint_input"()

declare i8* @"bigint_add"(i8* %".1", i8* %".2")

declare i8* @"bigint_sub"(i8* %".1", i8* %".2")

declare i8* @"bigint_mul"(i8* %".1", i8* %".2")

declare i8* @"bigint_div"(i8* %".1", i8* %".2")

declare i8* @"bigint_mod"(i8* %".1", i8* %".2")

declare i8* @"bigint_neg"(i8* %".1")

declare i32 @"bigint_cmp"(i8* %".1", i8* %".2")

declare void @"bigint_print"(i8* %".1")

declare i8* @"bigint_to_str"(i8* %".1")

declare void @"assign"(i8* %".1", i32 %".2", i64 %".3")

declare i64 @"dyn_as"(i8* %".1", i32 %".2")

declare i32 @"dyn_truthy"(i8* %".1")

declare void @"dyn_print"(i8* %".1")

declare i8* @"dyn_str"(i8* %".1")

declare {i32, i64}* @"dyn_slot_ptr"(i32 %".1")

declare void @"dyn_set"(i32 %".1", i32 %".2", i64 %".3")

declare i32 @"dyn_kind"(i32 %".1")

declare i64 @"dyn_get"(i32 %".1", i32 %".2")

declare i64 @"dyn_as_v"(i32 %".1", i64 %".2", i32 %".3")

declare i32 @"dyn_truthy_v"(i32 %".1", i64 %".2")

declare void @"dyn_print_v"(i32 %".1", i64 %".2")

declare i8* @"dyn_str_v"(i32 %".1", i64 %".2")

declare void @"dyn_op"({i32, i64}* %".1", i32 %".2", i32 %".3", i64 %".4", i32 %".5", i64 %".6")

declare void @"dyn_op1"({i32, i64}* %".1", i32 %".2", i32 %".3", i64 %".4")

declare {i32, i64}* @"dyn_list_get"(i64 %".1", i64 %".2")

declare void @"cpyte_array_register"(i8* %".1", i64 %".2")

declare i64 @"cpyte_array_len"(i8* %".1")

declare void @"cpyte_array_unregister"(i8* %".1")

declare void @"gc_init"()

declare i8* @"gc_malloc"(i64 %".1")

declare void @"gc_write_barrier"(i8* %".1", i8* %".2")

declare void @"gc_collect"()

declare void @"gc_start_thread"()

declare void @"gc_stop_thread"()

declare void @"gc_shutdown"()

@"_exc_type" = global i8* null
@"_exc_buf_ptr" = global i8* null
declare i32 @"setjmp"(i8* %".1") returns_twice

declare void @"longjmp"(i8* %".1", i32 %".2") noreturn

declare i32 @"strcmp"(i8* %".1", i8* %".2")

declare i32 @"glob_match"(i8* %".1", i8* %".2")

define i32 @"main"()
{
entry:
  %"dyn.binop.1" = alloca {i32, i64}
  %"dyn.binop" = alloca {i32, i64}
  %"count" = alloca i32
  %"n" = alloca i32
  %"moneys" = alloca {i32, i64}*
  %"a" = alloca i32
  call void @"gc_init"()
  call void @"gc_start_thread"()
  %".4" = call i32 @"input_int"()
  store i32 %".4", i32* %"a"
  %".6" = zext i32 16 to i64
  %".7" = mul i64 7, %".6"
  %".8" = call i8* @"gc_malloc"(i64 %".7")
  %".9" = bitcast i8* %".8" to {i32, i64}*
  %".10" = bitcast {i32, i64}* %".9" to i8*
  call void @"cpyte_array_register"(i8* %".10", i64 7)
  %".12" = sext i32 1 to i64
  %".13" = insertvalue {i32, i64} undef, i32 1, 0
  %".14" = insertvalue {i32, i64} %".13", i64 %".12", 1
  %".15" = getelementptr inbounds {i32, i64}, {i32, i64}* %".9", i64 0
  store {i32, i64} %".14", {i32, i64}* %".15"
  %".17" = sext i32 2 to i64
  %".18" = insertvalue {i32, i64} undef, i32 1, 0
  %".19" = insertvalue {i32, i64} %".18", i64 %".17", 1
  %".20" = getelementptr inbounds {i32, i64}, {i32, i64}* %".9", i64 1
  store {i32, i64} %".19", {i32, i64}* %".20"
  %".22" = sext i32 5 to i64
  %".23" = insertvalue {i32, i64} undef, i32 1, 0
  %".24" = insertvalue {i32, i64} %".23", i64 %".22", 1
  %".25" = getelementptr inbounds {i32, i64}, {i32, i64}* %".9", i64 2
  store {i32, i64} %".24", {i32, i64}* %".25"
  %".27" = sext i32 10 to i64
  %".28" = insertvalue {i32, i64} undef, i32 1, 0
  %".29" = insertvalue {i32, i64} %".28", i64 %".27", 1
  %".30" = getelementptr inbounds {i32, i64}, {i32, i64}* %".9", i64 3
  store {i32, i64} %".29", {i32, i64}* %".30"
  %".32" = sext i32 20 to i64
  %".33" = insertvalue {i32, i64} undef, i32 1, 0
  %".34" = insertvalue {i32, i64} %".33", i64 %".32", 1
  %".35" = getelementptr inbounds {i32, i64}, {i32, i64}* %".9", i64 4
  store {i32, i64} %".34", {i32, i64}* %".35"
  %".37" = sext i32 50 to i64
  %".38" = insertvalue {i32, i64} undef, i32 1, 0
  %".39" = insertvalue {i32, i64} %".38", i64 %".37", 1
  %".40" = getelementptr inbounds {i32, i64}, {i32, i64}* %".9", i64 5
  store {i32, i64} %".39", {i32, i64}* %".40"
  %".42" = sext i32 100 to i64
  %".43" = insertvalue {i32, i64} undef, i32 1, 0
  %".44" = insertvalue {i32, i64} %".43", i64 %".42", 1
  %".45" = getelementptr inbounds {i32, i64}, {i32, i64}* %".9", i64 6
  store {i32, i64} %".44", {i32, i64}* %".45"
  store {i32, i64}* %".9", {i32, i64}** %"moneys"
  store i32 6, i32* %"n"
  store i32 0, i32* %"count"
  br label %"while.cond"
while.cond:
  %"a.1" = load i32, i32* %"a"
  %".51" = icmp ne i32 %"a.1", 0
  br i1 %".51", label %"while.body", label %"while.end"
while.body:
  %"a.2" = load i32, i32* %"a"
  %".53" = sext i32 %"a.2" to i64
  %".54" = insertvalue {i32, i64} undef, i32 1, 0
  %".55" = insertvalue {i32, i64} %".54", i64 %".53", 1
  %".56" = extractvalue {i32, i64} %".55", 0
  %".57" = extractvalue {i32, i64} %".55", 1
  %"moneys.1" = load {i32, i64}*, {i32, i64}** %"moneys"
  %"n.1" = load i32, i32* %"n"
  %".58" = sext i32 %"n.1" to i64
  %".59" = getelementptr inbounds {i32, i64}, {i32, i64}* %"moneys.1", i64 %".58"
  %".60" = load {i32, i64}, {i32, i64}* %".59"
  %".61" = extractvalue {i32, i64} %".60", 0
  %".62" = extractvalue {i32, i64} %".60", 1
  call void @"dyn_op"({i32, i64}* %"dyn.binop", i32 10, i32 %".56", i64 %".57", i32 %".61", i64 %".62")
  %".64" = load {i32, i64}, {i32, i64}* %"dyn.binop"
  %".65" = extractvalue {i32, i64} %".64", 0
  %".66" = extractvalue {i32, i64} %".64", 1
  %".67" = call i32 @"dyn_truthy_v"(i32 %".65", i64 %".66")
  %".68" = trunc i32 %".67" to i1
  br i1 %".68", label %"then", label %"else"
while.end:
  %"count.2" = load i32, i32* %"count"
  call void @"print_int"(i32 %"count.2")
  call void @"gc_shutdown"()
  ret i32 0
then:
  %"a.3" = load i32, i32* %"a"
  %".70" = sext i32 %"a.3" to i64
  %".71" = insertvalue {i32, i64} undef, i32 1, 0
  %".72" = insertvalue {i32, i64} %".71", i64 %".70", 1
  %".73" = extractvalue {i32, i64} %".72", 0
  %".74" = extractvalue {i32, i64} %".72", 1
  %"moneys.2" = load {i32, i64}*, {i32, i64}** %"moneys"
  %"n.2" = load i32, i32* %"n"
  %".75" = sext i32 %"n.2" to i64
  %".76" = getelementptr inbounds {i32, i64}, {i32, i64}* %"moneys.2", i64 %".75"
  %".77" = load {i32, i64}, {i32, i64}* %".76"
  %".78" = extractvalue {i32, i64} %".77", 0
  %".79" = extractvalue {i32, i64} %".77", 1
  call void @"dyn_op"({i32, i64}* %"dyn.binop.1", i32 1, i32 %".73", i64 %".74", i32 %".78", i64 %".79")
  %".81" = load {i32, i64}, {i32, i64}* %"dyn.binop.1"
  %".82" = extractvalue {i32, i64} %".81", 0
  %".83" = extractvalue {i32, i64} %".81", 1
  %".84" = call i64 @"dyn_as_v"(i32 %".82", i64 %".83", i32 1)
  %".85" = trunc i64 %".84" to i32
  store i32 %".85", i32* %"a"
  %"count.1" = load i32, i32* %"count"
  %".87" = add i32 %"count.1", 1
  store i32 %".87", i32* %"count"
  br label %"endif"
else:
  %"n.3" = load i32, i32* %"n"
  %".90" = sub i32 %"n.3", 1
  store i32 %".90", i32* %"n"
  br label %"endif"
endif:
  br label %"while.cond"
}
