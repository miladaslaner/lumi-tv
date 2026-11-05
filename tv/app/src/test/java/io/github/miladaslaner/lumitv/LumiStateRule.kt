package io.github.miladaslaner.lumitv

import org.junit.rules.ExternalResource

/**
 * Resets the process-wide state ([AskController], [MessageBus], [Viewers], [AnswerActivity]'s statics) before and
 * after each test, so no test passes or fails because of what another left behind, or because of the order JUnit
 * happens to run them in.
 */
class LumiStateRule : ExternalResource() {
    override fun before() = resetAll()

    override fun after() = resetAll()

    private fun resetAll() {
        AskController.reset()
        MessageBus.reset()
        Viewers.reset()
        AnswerActivity.reset()
    }
}
